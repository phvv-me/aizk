import asyncio
from datetime import UTC, datetime, time, timedelta
from math import ceil
from typing import Literal

from patos import FrozenModel
from pydantic import ConfigDict

from .store import Artifact, Chunk, Usage
from .store.identity import OrganizationStanding, User
from .store.models.tables import UsageEvent

type Confidence = Literal["high", "medium", "low", "unavailable"]
type ETAStatus = Literal["complete", "estimating", "insufficient_history", "blocked"]
type ProcessingState = Literal["idle", "active", "delayed"]


class StatusView(FrozenModel):
    """Base for status models whose serialized defaults remain explicit."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class UsageSummary(StatusView):
    """Durable operation, item, transfer, and execution totals for one caller."""

    recalls: int = 0
    remembers: int = 0
    files: int = 0
    shares: int = 0
    artifact_reads: int = 0
    requests: int = 0
    items: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    uploaded_bytes: int = 0
    downloaded_bytes: int = 0
    duration_ms: float = 0.0


class UsagePoint(StatusView):
    """One operation's durable UTC daily usage bucket."""

    bucket: datetime
    operation: UsageEvent.Operation
    requests: int
    items: int
    request_bytes: int
    response_bytes: int
    duration_ms: float


class UsageReport(StatusView):
    """Selected-period and lifetime durable usage for the authenticated caller."""

    generated_at: datetime
    recorded_through: datetime
    days: int
    start: datetime
    summary: UsageSummary
    lifetime: UsageSummary
    points: tuple[UsagePoint, ...] = ()

    @classmethod
    async def load(cls, user: User, days: int = 30) -> UsageReport:
        """Load complete UTC calendar days and the caller's lifetime summary."""
        generated_at = datetime.now(UTC)
        start_date = generated_at.date() - timedelta(days=days - 1)
        start = datetime.combine(start_date, time.min, tzinfo=UTC)
        (summary,) = await user.exec[UsageSummary](Usage.Event.report_totals(start))
        (lifetime,) = await user.exec[UsageSummary](Usage.Event.report_totals())
        points = await user.exec[UsagePoint](Usage.Event.daily_since(start))
        return cls(
            generated_at=generated_at,
            recorded_through=generated_at,
            days=days,
            start=start,
            summary=summary,
            lifetime=lifetime,
            points=tuple(points),
        )


class UsageStatus(StatusView):
    """Compact period and lifetime usage without daily chart buckets."""

    generated_at: datetime
    recorded_through: datetime
    days: int
    start: datetime
    summary: UsageSummary
    lifetime: UsageSummary

    @classmethod
    def from_report(cls, report: UsageReport) -> UsageStatus:
        """Keep status context bounded while the detailed usage report retains points."""
        return cls(**report.model_dump(exclude={"points"}))


class StageEstimate(StatusView):
    """One processing stage's visible workload and recent drain estimate."""

    key: str
    queued: int = 0
    running: int | None = None
    failed: int | None = None
    completed_1h: int = 0
    completed_24h: int = 0
    progress_percent: int = 0
    throughput_per_hour: float = 0.0
    throughput_window_hours: int | None = None
    lower_seconds: int | None = None
    upper_seconds: int | None = None
    confidence: Confidence = "unavailable"
    eta_status: ETAStatus = "insufficient_history"
    oldest_at: datetime | None = None

    @classmethod
    def estimate(
        cls,
        key: str,
        queued: int,
        running: int | None,
        failed: int | None,
        completed_1h: int,
        completed_6h: int,
        completed_24h: int,
        oldest_at: datetime | None = None,
    ) -> StageEstimate:
        """Estimate clearance from the current measured rate with bounded uncertainty."""
        backlog = queued + (running or 0)
        failures = failed or 0
        workload = backlog + failures + completed_24h
        progress = round(completed_24h / workload * 100) if workload else 100
        required_sample = min(20, max(1, backlog))
        if completed_1h >= required_sample:
            current_sample, window = completed_1h, 1
        elif completed_6h >= required_sample:
            current_sample, window = completed_6h, 6
        else:
            current_sample, window = (completed_1h, 1) if completed_1h > 0 else (completed_6h, 6)
        current_rate = current_sample / window
        if failures:
            lower = upper = None
            confidence: Confidence = "unavailable"
            eta_status: ETAStatus = "blocked"
        elif backlog == 0:
            lower = upper = 0
            confidence = "high"
            eta_status = "complete"
        elif current_sample < required_sample:
            lower = upper = None
            confidence = "unavailable"
            eta_status = "insufficient_history"
        else:
            historical_rate = completed_24h / 24
            drift = (
                max(current_rate, historical_rate) / min(current_rate, historical_rate)
                if historical_rate > 0
                else float("inf")
            )
            if current_sample >= 100 and drift <= 1.5:
                confidence = "high"
                slow_rate, fast_rate = current_rate * 0.8, current_rate * 1.2
            elif current_sample >= 20 and drift <= 3:
                confidence = "medium"
                slow_rate, fast_rate = current_rate * 2 / 3, current_rate * 1.5
            else:
                confidence = "low"
                slow_rate, fast_rate = current_rate * 0.5, current_rate * 2
            lower = ceil(backlog / fast_rate * 3600)
            upper = ceil(backlog / slow_rate * 3600)
            eta_status = "estimating"
        return cls(
            key=key,
            queued=queued,
            running=running,
            failed=failed,
            completed_1h=completed_1h,
            completed_24h=completed_24h,
            progress_percent=progress,
            throughput_per_hour=current_rate,
            throughput_window_hours=window if current_sample else None,
            lower_seconds=lower,
            upper_seconds=upper,
            confidence=confidence,
            eta_status=eta_status,
            oldest_at=oldest_at,
        )


class ArtifactProcessingRecord(FrozenModel):
    """Database-shaped caller-visible artifact processing aggregate."""

    queued: int
    running: int
    failed: int
    completed_1h: int
    completed_6h: int
    completed_24h: int
    oldest_at: datetime | None


class ChunkProcessingRecord(FrozenModel):
    """Database-shaped caller-visible source-section processing aggregate."""

    queued: int
    completed_1h: int
    completed_6h: int
    completed_24h: int


class ProcessingStatus(StatusView):
    """Caller-visible processing stages and honest ETA ranges."""

    generated_at: datetime
    state: ProcessingState
    stages: tuple[StageEstimate, ...]
    recallable_lower_seconds: int | None = None
    recallable_upper_seconds: int | None = None
    enriched_lower_seconds: int | None = None
    enriched_upper_seconds: int | None = None

    @classmethod
    async def load(cls, user: User) -> ProcessingStatus:
        """Load visible conversion and graph workloads with recent throughput estimates."""
        now = datetime.now(UTC)
        one_hour_ago = now - timedelta(hours=1)
        six_hours_ago = now - timedelta(hours=6)
        day_ago = now - timedelta(days=1)
        (artifacts,) = await user.exec[ArtifactProcessingRecord](
            Artifact.Content.processing_counts(one_hour_ago, six_hours_ago, day_ago)
        )
        (chunks,) = await user.exec[ChunkProcessingRecord](
            Chunk.processing_counts(one_hour_ago, six_hours_ago, day_ago)
        )
        conversion = StageEstimate.estimate(
            "conversion",
            artifacts.queued,
            artifacts.running,
            artifacts.failed,
            artifacts.completed_1h,
            artifacts.completed_6h,
            artifacts.completed_24h,
            artifacts.oldest_at,
        )
        projection = StageEstimate.estimate(
            "graph_projection",
            chunks.queued,
            None,
            None,
            chunks.completed_1h,
            chunks.completed_6h,
            chunks.completed_24h,
        )
        stages = (conversion, projection)
        backlog = sum(stage.queued + (stage.running or 0) for stage in stages)
        failures = sum(stage.failed or 0 for stage in stages)
        delayed = any(
            (stage.failed or 0) > 0
            or (stage.queued + (stage.running or 0) > 0 and stage.confidence == "unavailable")
            for stage in stages
        )
        state: ProcessingState = (
            "idle" if backlog == 0 and failures == 0 else "delayed" if delayed else "active"
        )
        conversion_clear = (
            conversion.queued + (conversion.running or 0) == 0 and (conversion.failed or 0) == 0
        )
        return cls(
            generated_at=now,
            state=state,
            stages=stages,
            recallable_lower_seconds=conversion.lower_seconds,
            recallable_upper_seconds=conversion.upper_seconds,
            # Pending conversions create chunks that do not exist yet. Predicting full
            # enrichment before that downstream workload materializes would undercount it.
            enriched_lower_seconds=projection.lower_seconds if conversion_clear else None,
            enriched_upper_seconds=projection.upper_seconds if conversion_clear else None,
        )


class OrganizationStatus(StatusView):
    """One caller-visible organization and its current effective authority."""

    name: str
    description: str | None = None
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    writable: bool = False
    public: bool = False

    @classmethod
    def from_standing(cls, organization: OrganizationStanding) -> OrganizationStatus:
        """Copy only directory-safe organization standing into the status report."""
        return cls(
            name=organization.name,
            description=organization.description,
            roles=organization.roles,
            permissions=organization.permissions,
            writable=organization.writable,
            public=organization.public,
        )


class CallerStatus(StatusView):
    """Directory-safe caller identity and current Logto-derived authority."""

    name: str | None = None
    username: str | None = None
    avatar: str | None = None
    label: str | None = None
    roles: tuple[str, ...] = ()
    anonymous: bool = False
    organizations: tuple[OrganizationStatus, ...] = ()

    @classmethod
    def from_user(cls, user: User) -> CallerStatus:
        """Present identity without stable identifiers or internal scope UUIDs."""
        return cls(
            name=user.name,
            username=user.username,
            avatar=user.avatar,
            label=user.label,
            roles=user.roles,
            anonymous=user.is_anonymous(),
            organizations=tuple(
                OrganizationStatus.from_standing(organization)
                for organization in sorted(
                    user.organizations,
                    key=lambda item: item.name.casefold(),
                )
            ),
        )


class StatusReport(StatusView):
    """Identity, durable usage, and processing health for one caller."""

    generated_at: datetime
    caller: CallerStatus
    usage: UsageStatus
    processing: ProcessingStatus

    @classmethod
    async def load(cls, user: User, days: int = 30) -> StatusReport:
        """Load independent caller-bound usage and processing reads concurrently."""
        usage, processing = await asyncio.gather(
            UsageReport.load(user, days),
            ProcessingStatus.load(user),
        )
        return cls(
            generated_at=datetime.now(UTC),
            caller=CallerStatus.from_user(user),
            usage=UsageStatus.from_report(usage),
            processing=processing,
        )
