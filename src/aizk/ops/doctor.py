from datetime import UTC, datetime, timedelta
from hashlib import sha256
from re import fullmatch, sub
from typing import Literal
from uuid import UUID

import asyncpg
from patos import FrozenModel
from pydantic import UUID7, ValidationError
from sqlalchemy import and_, or_
from sqlmodel import select

from ..background.jobs.conversion import DoclingConversionJob
from ..background.jobs.models import ArtifactConversionJob
from ..background.queue import Queue
from ..config import settings
from ..store import Artifact
from ..store.identity import User
from ..store.models.tables import ArtifactContent

type QueueIssueKind = Literal["stale_picked", "long_running_picked"]
type ConversionState = Literal[
    "failed",
    "active_terminal_queue",
    "active_queued",
    "active_stale",
    "active_fresh",
]
type ConversionQueueActivity = Literal["queued", "picked"]
type Severity = Literal["error", "warning", "info"]


class ErrorIdentity(FrozenModel):
    """A non-reversible operator-safe identity for one stored error."""

    type: str
    fingerprint: str
    sanitized_message: str | None = None


class QueueIssue(FrozenModel):
    """One current retained failure or unhealthy picked queue job."""

    id: int
    entrypoint: str
    kind: QueueIssueKind
    updated_at: datetime
    heartbeat_at: datetime
    age_seconds: int
    attempts: int
    latest_error: ErrorIdentity | None
    retry_guidance: str


class QueueFailureGroup(FrozenModel):
    """Current retained failures grouped without exposing exception messages."""

    entrypoint: str
    error: ErrorIdentity | None
    count: int
    attempts_min: int
    attempts_max: int
    oldest_at: datetime
    newest_at: datetime
    remediation: str


class ExceptionHistoryGroup(FrozenModel):
    """Recent exception history aggregated separately from current failures."""

    entrypoint: str
    status: Literal["exception", "failed"]
    error: ErrorIdentity | None
    count: int
    oldest_at: datetime
    newest_at: datetime


class ConversionQueueFailure(FrozenModel):
    """The retained terminal PgQueuer job that owns one artifact conversion."""

    job_id: int
    attempts: int
    error: ErrorIdentity | None


class ConversionDiagnostic(FrozenModel):
    """One failed or active artifact conversion without caller-visible source text."""

    artifact_id: UUID7
    content_id: UUID7
    artifact_name: str
    state: ConversionState
    updated_at: datetime
    age_seconds: int
    error: ErrorIdentity | None
    queue_failure: ConversionQueueFailure | None = None
    queue_status: Literal["queued", "picked", "failed"] | None = None
    retry_guidance: str


class DoctorSummary(FrozenModel):
    """Complete issue counts, independent from the bounded detail lists."""

    current_failed_jobs: int = 0
    stale_picked_jobs: int = 0
    long_running_picked_jobs: int = 0
    recent_exception_events: int = 0
    failed_conversions: int = 0
    orphaned_active_conversions: int = 0
    queued_active_conversions: int = 0
    fresh_active_conversions: int = 0
    stale_active_conversions: int = 0


class DoctorFinding(FrozenModel):
    """One concise operational conclusion and its next safe action."""

    severity: Severity
    code: str
    count: int
    message: str
    action: str


class DoctorReport(FrozenModel):
    """Read-only queue, retry history, and artifact conversion diagnosis."""

    generated_at: datetime
    healthy: bool
    stale_after_seconds: int
    long_running_after_seconds: int
    history_seconds: int
    detail_limit: int
    error_messages_included: bool
    summary: DoctorSummary
    findings: tuple[DoctorFinding, ...] = ()
    queue_failure_groups: tuple[QueueFailureGroup, ...] = ()
    queue_issues: tuple[QueueIssue, ...] = ()
    recent_exception_groups: tuple[ExceptionHistoryGroup, ...] = ()
    conversions: tuple[ConversionDiagnostic, ...] = ()


class QueueDiagnostics:
    """Read current and historical PgQueuer state through its discovered schema."""

    def __init__(
        self,
        stale_after: timedelta,
        long_running_after: timedelta,
        history: timedelta,
        limit: int,
        include_messages: bool,
    ) -> None:
        self.stale_after = stale_after
        self.long_running_after = long_running_after
        self.history = history
        self.limit = limit
        self.include_messages = include_messages

    async def load(
        self,
        now: datetime,
    ) -> tuple[
        DoctorSummary,
        tuple[QueueFailureGroup, ...],
        tuple[QueueIssue, ...],
        tuple[ExceptionHistoryGroup, ...],
        dict[UUID, ConversionQueueFailure],
        dict[UUID, ConversionQueueActivity],
    ]:
        """Load complete counts and bounded details from PgQueuer."""
        stale_before = now - self.stale_after
        long_running_before = now - self.long_running_after
        history_since = now - self.history
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            names = queue.queries.qbe.settings
            counts = await queue.connection.fetchrow(
                f"""
                SELECT
                    count(*) FILTER (WHERE status = 'failed') AS current_failed_jobs,
                    count(*) FILTER (
                        WHERE status = 'picked' AND heartbeat < $1
                    ) AS stale_picked_jobs,
                    count(*) FILTER (
                        WHERE status = 'picked'
                          AND heartbeat >= $1
                          AND updated < $2
                    ) AS long_running_picked_jobs
                FROM {names.queue_table}
                """,
                stale_before,
                long_running_before,
            )
            assert counts is not None
            recent_exception_events = await queue.connection.fetchval(
                f"""
                SELECT count(*)
                FROM {names.queue_table_log}
                WHERE status IN ('exception', 'failed') AND created >= $1
                """,
                history_since,
            )
            issue_rows = await queue.connection.fetch(
                f"""
                SELECT
                    job.id,
                    job.entrypoint,
                    CASE
                        WHEN job.heartbeat < $1 THEN 'stale_picked'
                        ELSE 'long_running_picked'
                    END AS kind,
                    job.updated AS updated_at,
                    job.heartbeat AS heartbeat_at,
                    EXTRACT(
                        EPOCH FROM (
                            $3 - CASE
                                WHEN job.status = 'picked' AND job.heartbeat < $1
                                    THEN job.heartbeat
                                ELSE job.updated
                            END
                        )
                    )::bigint AS age_seconds,
                    job.attempts,
                    latest.exception_type,
                    latest.exception_message
                FROM {names.queue_table} AS job
                LEFT JOIN LATERAL (
                    SELECT
                        log.traceback->>'exception_type' AS exception_type,
                        log.traceback->>'exception_message' AS exception_message
                    FROM {names.queue_table_log} AS log
                    WHERE log.job_id = job.id
                      AND log.status IN ('exception', 'failed')
                      AND log.traceback IS NOT NULL
                    ORDER BY log.created DESC, log.id DESC
                    LIMIT 1
                ) AS latest ON true
                WHERE job.status = 'picked'
                  AND (job.heartbeat < $1 OR job.updated < $2)
                ORDER BY
                    CASE
                        WHEN job.heartbeat < $1 THEN 0
                        ELSE 1
                    END,
                    job.updated,
                    job.id
                LIMIT $4
                """,
                stale_before,
                long_running_before,
                now,
                self.limit,
            )
            failure_rows = await queue.connection.fetch(
                f"""
                SELECT
                    job.entrypoint,
                    latest.exception_type,
                    latest.exception_message,
                    count(*) AS count,
                    min(job.attempts) AS attempts_min,
                    max(job.attempts) AS attempts_max,
                    min(job.updated) AS oldest_at,
                    max(job.updated) AS newest_at
                FROM {names.queue_table} AS job
                LEFT JOIN LATERAL (
                    SELECT
                        log.traceback->>'exception_type' AS exception_type,
                        log.traceback->>'exception_message' AS exception_message
                    FROM {names.queue_table_log} AS log
                    WHERE log.job_id = job.id
                      AND log.status IN ('exception', 'failed')
                      AND log.traceback IS NOT NULL
                    ORDER BY log.created DESC, log.id DESC
                    LIMIT 1
                ) AS latest ON true
                WHERE job.status = 'failed'
                GROUP BY
                    job.entrypoint,
                    latest.exception_type,
                    latest.exception_message
                ORDER BY count(*) DESC, job.entrypoint, latest.exception_type
                LIMIT $1
                """,
                self.limit,
            )
            exception_rows = await queue.connection.fetch(
                f"""
                SELECT
                    log.entrypoint,
                    log.status::text AS status,
                    log.traceback->>'exception_type' AS exception_type,
                    log.traceback->>'exception_message' AS exception_message,
                    count(*) AS count,
                    min(log.created) AS oldest_at,
                    max(log.created) AS newest_at
                FROM {names.queue_table_log} AS log
                WHERE log.status IN ('exception', 'failed')
                  AND log.created >= $1
                  AND log.traceback IS NOT NULL
                GROUP BY
                    log.entrypoint,
                    log.status,
                    log.traceback->>'exception_type',
                    log.traceback->>'exception_message'
                ORDER BY count(*) DESC, log.entrypoint, log.status
                LIMIT $2
                """,
                history_since,
                self.limit,
            )
            conversion_rows = await queue.connection.fetch(
                f"""
                SELECT
                    job.id,
                    job.payload,
                    job.status::text AS status,
                    job.attempts,
                    latest.exception_type,
                    latest.exception_message
                FROM {names.queue_table} AS job
                LEFT JOIN LATERAL (
                    SELECT
                        log.traceback->>'exception_type' AS exception_type,
                        log.traceback->>'exception_message' AS exception_message
                    FROM {names.queue_table_log} AS log
                    WHERE log.job_id = job.id
                      AND log.status IN ('exception', 'failed')
                      AND log.traceback IS NOT NULL
                    ORDER BY log.created DESC, log.id DESC
                    LIMIT 1
                ) AS latest ON true
                WHERE job.status IN ('queued', 'picked', 'failed')
                  AND job.entrypoint = $1
                  AND job.payload IS NOT NULL
                ORDER BY
                    CASE job.status
                        WHEN 'failed' THEN 0
                        WHEN 'picked' THEN 1
                        ELSE 2
                    END,
                    job.updated,
                    job.id
                """,
                DoclingConversionJob.entrypoint,
            )
        conversion_failures, conversion_activity = self.conversion_jobs(conversion_rows)
        summary = DoctorSummary(
            current_failed_jobs=counts["current_failed_jobs"],
            stale_picked_jobs=counts["stale_picked_jobs"],
            long_running_picked_jobs=counts["long_running_picked_jobs"],
            recent_exception_events=recent_exception_events,
        )
        return (
            summary,
            tuple(self.failure_group(row) for row in failure_rows),
            tuple(self.issue(row) for row in issue_rows),
            tuple(self.exception_group(row) for row in exception_rows),
            conversion_failures,
            conversion_activity,
        )

    def issue(self, row: asyncpg.Record) -> QueueIssue:
        """Translate one current queue problem into safe operator guidance."""
        kind = row["kind"]
        guidance = {
            "stale_picked": ("Confirm its worker is gone before requeueing this abandoned lease."),
            "long_running_picked": (
                "Inspect downstream latency and worker logs before interrupting this live lease."
            ),
        }[kind]
        return QueueIssue(
            id=row["id"],
            entrypoint=row["entrypoint"],
            kind=kind,
            updated_at=row["updated_at"],
            heartbeat_at=row["heartbeat_at"],
            age_seconds=max(0, row["age_seconds"]),
            attempts=row["attempts"],
            latest_error=error_identity(
                row["exception_type"],
                row["exception_message"],
                self.include_messages,
            ),
            retry_guidance=guidance,
        )

    def failure_group(self, row: asyncpg.Record) -> QueueFailureGroup:
        """Translate current failures into one privacy-safe operational group."""
        remediation = (
            "Fix this error group, then run `aizk admin queue retry conversion`."
            if row["entrypoint"] == DoclingConversionJob.entrypoint
            else "Fix this error group before requeueing its retained jobs."
        )
        return QueueFailureGroup(
            entrypoint=row["entrypoint"],
            error=error_identity(
                row["exception_type"],
                row["exception_message"],
                self.include_messages,
            ),
            count=row["count"],
            attempts_min=row["attempts_min"],
            attempts_max=row["attempts_max"],
            oldest_at=row["oldest_at"],
            newest_at=row["newest_at"],
            remediation=remediation,
        )

    def exception_group(self, row: asyncpg.Record) -> ExceptionHistoryGroup:
        """Translate historical events into one privacy-safe aggregate."""
        return ExceptionHistoryGroup(
            entrypoint=row["entrypoint"],
            status=row["status"],
            error=error_identity(
                row["exception_type"],
                row["exception_message"],
                self.include_messages,
            ),
            count=row["count"],
            oldest_at=row["oldest_at"],
            newest_at=row["newest_at"],
        )

    def conversion_failures(
        self,
        rows: list[asyncpg.Record],
    ) -> dict[UUID, ConversionQueueFailure]:
        """Index valid content-id dedupe keys from terminal conversion jobs."""
        failures: dict[UUID, ConversionQueueFailure] = {}
        for row in rows:
            try:
                content_id = ArtifactConversionJob.decode(row["payload"]).artifact_content_id
            except TypeError, ValueError, ValidationError:
                continue
            failures[content_id] = ConversionQueueFailure(
                job_id=row["id"],
                attempts=row["attempts"],
                error=error_identity(
                    row["exception_type"],
                    row["exception_message"],
                    self.include_messages,
                ),
            )
        return failures

    def conversion_jobs(
        self,
        rows: list[asyncpg.Record],
    ) -> tuple[
        dict[UUID, ConversionQueueFailure],
        dict[UUID, ConversionQueueActivity],
    ]:
        """Index terminal and active conversion jobs by their durable content ID."""
        failures = self.conversion_failures([row for row in rows if row["status"] == "failed"])
        activity: dict[UUID, ConversionQueueActivity] = {}
        for row in rows:
            if row["status"] == "failed":
                continue
            try:
                content_id = ArtifactConversionJob.decode(row["payload"]).artifact_content_id
            except TypeError, ValueError, ValidationError:
                continue
            activity[content_id] = row["status"]
        return failures, activity


class ConversionDiagnostics:
    """Read failed and active artifact conversions through the owner maintenance role."""

    def __init__(self, stale_after: timedelta, limit: int, include_messages: bool) -> None:
        self.stale_after = stale_after
        self.limit = limit
        self.include_messages = include_messages

    async def load(
        self,
        now: datetime,
        queue_failures: dict[UUID, ConversionQueueFailure],
        queue_activity: dict[UUID, ConversionQueueActivity],
    ) -> tuple[DoctorSummary, tuple[ConversionDiagnostic, ...]]:
        """Load complete conversion counts and bounded identifier-only details."""
        stale_before = now - self.stale_after
        failed = ArtifactContent.State.failed
        processing = ArtifactContent.State.processing
        orphaned_ids = tuple(queue_failures)
        orphaned = ArtifactContent.id.in_(orphaned_ids)
        queued_ids = tuple(
            content_id for content_id, status in queue_activity.items() if status == "queued"
        )
        picked_ids = tuple(
            content_id for content_id, status in queue_activity.items() if status == "picked"
        )
        queued = ArtifactContent.id.in_(queued_ids)
        picked = ArtifactContent.id.in_(picked_ids)
        no_activity = ArtifactContent.id.not_in((*queued_ids, *picked_ids))
        protected_ids = (*orphaned_ids, *queued_ids, *picked_ids)
        unowned = ArtifactContent.id.not_in(protected_ids)
        async with User.system().owner as session:
            counts = (
                await session.exec(
                    select(
                        ArtifactContent.id.count()
                        .filter(
                            ArtifactContent.state == failed,
                            no_activity,
                        )
                        .label("failed_conversions"),
                        ArtifactContent.id.count()
                        .filter(
                            ArtifactContent.state == processing,
                            orphaned,
                        )
                        .label("orphaned_active_conversions"),
                        ArtifactContent.id.count()
                        .filter(
                            ArtifactContent.state.in_((failed, processing)),
                            queued,
                        )
                        .label("queued_active_conversions"),
                        ArtifactContent.id.count()
                        .filter(
                            or_(
                                and_(
                                    ArtifactContent.state.in_((failed, processing)),
                                    picked,
                                ),
                                and_(
                                    ArtifactContent.state == processing,
                                    unowned,
                                    ArtifactContent.updated_at >= stale_before,
                                ),
                            ),
                        )
                        .label("fresh_active_conversions"),
                    )
                )
            ).one()
            stale_count = (
                await session.exec(
                    select(
                        ArtifactContent.id.count()
                        .filter(
                            ArtifactContent.state == processing,
                            unowned,
                            ArtifactContent.updated_at < stale_before,
                        )
                        .label("stale_active_conversions")
                    )
                )
            ).one()
            rows = (
                await session.exec(
                    select(Artifact, ArtifactContent)
                    .join(
                        ArtifactContent,
                        ArtifactContent.__table__.c.artifact_id == Artifact.__table__.c.id,
                    )
                    .where(ArtifactContent.__table__.c.state.in_((failed, processing)))
                    .order_by(
                        ArtifactContent.__table__.c.state.desc(),
                        ArtifactContent.__table__.c.updated_at,
                        ArtifactContent.__table__.c.id,
                    )
                    .limit(self.limit)
                )
            ).all()
        failed_count, orphaned_count, queued_count, fresh_count = counts
        summary = DoctorSummary(
            failed_conversions=failed_count,
            orphaned_active_conversions=orphaned_count,
            queued_active_conversions=queued_count,
            fresh_active_conversions=fresh_count,
            stale_active_conversions=stale_count,
        )
        return summary, tuple(
            self.diagnostic(
                content,
                artifact.name,
                now,
                stale_before,
                queue_failures.get(content.id),
                queue_activity.get(content.id),
            )
            for artifact, content in rows
        )

    def diagnostic(
        self,
        row: ArtifactContent,
        artifact_name: str,
        now: datetime,
        stale_before: datetime,
        queue_failure: ConversionQueueFailure | None,
        queue_activity: ConversionQueueActivity | None = None,
    ) -> ConversionDiagnostic:
        """Classify one conversion with live queue ownership taking precedence."""
        if queue_activity == "queued":
            state: ConversionState = "active_queued"
            guidance = "No retry is needed while this conversion waits in PgQueuer."
        elif queue_activity == "picked":
            state = "active_fresh"
            guidance = "No retry is needed while PgQueuer has an active worker lease."
        elif row.state == ArtifactContent.State.failed:
            state = "failed"
            guidance = "Fix the stored conversion error before retrying this original."
        elif queue_failure is not None:
            state = "active_terminal_queue"
            guidance = (
                "This durable active state has a terminal queue job. Fix its queue error "
                "before reconciling and retrying the conversion."
            )
        elif row.updated_at < stale_before:
            state = "active_stale"
            guidance = "Confirm no worker owns it before reconciling this abandoned conversion."
        else:
            state = "active_fresh"
            guidance = "No retry is needed while this conversion continues to update."
        return ConversionDiagnostic(
            artifact_id=row.artifact_id,
            content_id=row.id,
            artifact_name=artifact_name,
            state=state,
            updated_at=row.updated_at,
            age_seconds=max(0, int((now - row.updated_at).total_seconds())),
            error=error_identity(
                "ArtifactConversionError",
                row.error,
                self.include_messages,
            ),
            queue_failure=queue_failure,
            queue_status=("failed" if queue_failure is not None else queue_activity),
            retry_guidance=guidance,
        )


class AizkDoctor:
    """Orchestrate read-only queue and artifact conversion diagnostics."""

    def __init__(
        self,
        stale_after: timedelta,
        long_running_after: timedelta,
        history: timedelta,
        limit: int,
        include_messages: bool = False,
    ) -> None:
        if min(stale_after, long_running_after, history) <= timedelta(0):
            raise ValueError("doctor time windows must be positive")
        if limit < 1:
            raise ValueError("doctor detail limit must be positive")
        self.stale_after = stale_after
        self.long_running_after = long_running_after
        self.history = history
        self.limit = limit
        self.include_messages = include_messages

    async def diagnose(self, now: datetime | None = None) -> DoctorReport:
        """Build one deterministic report without changing queue or conversion state."""
        generated_at = now or datetime.now(UTC)
        (
            queue_summary,
            queue_failure_groups,
            queue_issues,
            recent_exception_groups,
            conversion_failures,
            conversion_activity,
        ) = await QueueDiagnostics(
            self.stale_after,
            self.long_running_after,
            self.history,
            self.limit,
            self.include_messages,
        ).load(generated_at)
        conversion_summary, conversions = await ConversionDiagnostics(
            self.long_running_after,
            self.limit,
            self.include_messages,
        ).load(generated_at, conversion_failures, conversion_activity)
        summary = DoctorSummary(
            **{
                **queue_summary.model_dump(),
                **conversion_summary.model_dump(
                    include={
                        "failed_conversions",
                        "orphaned_active_conversions",
                        "queued_active_conversions",
                        "fresh_active_conversions",
                        "stale_active_conversions",
                    }
                ),
            }
        )
        findings = self.findings(summary)
        return DoctorReport(
            generated_at=generated_at,
            healthy=not any(finding.severity == "error" for finding in findings),
            stale_after_seconds=int(self.stale_after.total_seconds()),
            long_running_after_seconds=int(self.long_running_after.total_seconds()),
            history_seconds=int(self.history.total_seconds()),
            detail_limit=self.limit,
            error_messages_included=self.include_messages,
            summary=summary,
            findings=findings,
            queue_failure_groups=queue_failure_groups,
            queue_issues=queue_issues,
            recent_exception_groups=recent_exception_groups,
            conversions=conversions,
        )

    @staticmethod
    def findings(summary: DoctorSummary) -> tuple[DoctorFinding, ...]:
        """Explain current blockers separately from recovered exception history."""
        candidates = (
            (
                summary.current_failed_jobs,
                DoctorFinding(
                    severity="error",
                    code="queue_failed",
                    count=summary.current_failed_jobs,
                    message="Retained queue jobs are currently failed.",
                    action="Fix each latest error before requeueing its job.",
                ),
            ),
            (
                summary.stale_picked_jobs,
                DoctorFinding(
                    severity="error",
                    code="queue_stale",
                    count=summary.stale_picked_jobs,
                    message="Picked jobs have stopped heartbeating.",
                    action="Confirm their workers are gone before requeueing abandoned leases.",
                ),
            ),
            (
                summary.long_running_picked_jobs,
                DoctorFinding(
                    severity="warning",
                    code="queue_long_running",
                    count=summary.long_running_picked_jobs,
                    message="Picked jobs exceed the expected runtime but still heartbeat.",
                    action="Inspect downstream latency and worker logs before interrupting them.",
                ),
            ),
            (
                summary.failed_conversions,
                DoctorFinding(
                    severity="error",
                    code="conversion_failed",
                    count=summary.failed_conversions,
                    message="Artifact conversions have durable failed state.",
                    action="Fix their stored errors before retrying the originals.",
                ),
            ),
            (
                summary.orphaned_active_conversions,
                DoctorFinding(
                    severity="error",
                    code="conversion_terminal_queue",
                    count=summary.orphaned_active_conversions,
                    message=(
                        "Artifact conversions look active but their queue jobs are terminal."
                    ),
                    action=(
                        "Fix each queue error, reconcile the durable state, then run "
                        "`aizk admin queue retry conversion`."
                    ),
                ),
            ),
            (
                summary.stale_active_conversions,
                DoctorFinding(
                    severity="error",
                    code="conversion_stale",
                    count=summary.stale_active_conversions,
                    message="Artifact conversions remain active without a recent update.",
                    action="Confirm no worker owns them before reconciling their state.",
                ),
            ),
            (
                summary.recent_exception_events,
                DoctorFinding(
                    severity="info",
                    code="recent_exception_history",
                    count=summary.recent_exception_events,
                    message="Recent queue exception history is present.",
                    action=(
                        "Use the event list for context. Recovered history is not a current "
                        "failure."
                    ),
                ),
            ),
        )
        return tuple(finding for count, finding in candidates if count)


def error_identity(
    error_type: str | None,
    message: str | None,
    include_message: bool = False,
) -> ErrorIdentity | None:
    """Build a safe type and fingerprint without returning stored exception text."""
    if not error_type and not message:
        return None
    safe_type = (
        error_type
        if error_type and fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", error_type)
        else "UnknownError"
    )
    normalized = " ".join((message or "").split())
    fingerprint = sha256(f"{safe_type}\0{normalized}".encode()).hexdigest()[:16]
    sanitized = sub(r"""(['"])(?:(?!\1).)*\1""", r"\1[redacted]\1", normalized)
    return ErrorIdentity(
        type=safe_type,
        fingerprint=fingerprint,
        sanitized_message=sanitized[:500] if include_message else None,
    )


async def doctor(
    stale_minutes: int = 15,
    long_running_minutes: int = 60,
    history_hours: int = 24,
    limit: int = 50,
    show_error_messages: bool = False,
) -> DoctorReport:
    """Run the read-only operator diagnosis with explicit bounded windows."""
    return await AizkDoctor(
        stale_after=timedelta(minutes=stale_minutes),
        long_running_after=timedelta(minutes=long_running_minutes),
        history=timedelta(hours=history_hours),
        limit=limit,
        include_messages=show_error_messages,
    ).diagnose()
