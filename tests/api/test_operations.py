from datetime import UTC, datetime, timedelta
from json import loads
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import dbutil
import pytest
from id_factory import uuid5
from sqlalchemy.sql.selectable import SelectBase

import aizk.api.operations as operations
from aizk.api.operations import (
    ArtifactProcessingRecord,
    ChunkProcessingRecord,
    ProcessingReport,
    ProcessingUpdates,
    StageEstimate,
    UsageReport,
)
from aizk.store import Artifact, Chunk, Usage
from aizk.store.identity import User
from aizk.usage import UsageAccountingJob, UsageCapture


def test_stage_estimate_rejects_a_stale_daily_rate_when_current_history_is_sparse() -> None:
    estimate = StageEstimate.estimate(
        "graph_projection",
        queued=10_733,
        running=0,
        failed=0,
        completed_1h=0,
        completed_6h=7,
        completed_24h=1828,
    )

    assert estimate.eta_status == "insufficient_history"
    assert estimate.confidence == "unavailable"
    assert estimate.lower_seconds is None
    assert estimate.upper_seconds is None
    assert estimate.throughput_per_hour == pytest.approx(7 / 6)
    assert estimate.throughput_window_hours == 6


@pytest.mark.parametrize(
    ("queued", "completed_1h", "completed_24h", "confidence", "lower", "upper"),
    [
        (120, 120, 2400, "high", 3000, 4500),
        (30, 30, 240, "medium", 2400, 5400),
        (2, 5, 100, "low", 720, 2880),
    ],
)
def test_stage_estimate_bounds_uncertainty_around_the_current_rate(
    queued: int,
    completed_1h: int,
    completed_24h: int,
    confidence: str,
    lower: int,
    upper: int,
) -> None:
    estimate = StageEstimate.estimate(
        "graph_projection",
        queued=queued,
        running=0,
        failed=0,
        completed_1h=completed_1h,
        completed_6h=completed_1h,
        completed_24h=completed_24h,
    )

    assert estimate.eta_status == "estimating"
    assert estimate.confidence == confidence
    assert estimate.lower_seconds == lower
    assert estimate.upper_seconds == upper
    assert upper <= lower * 4
    assert estimate.throughput_per_hour == completed_1h
    assert estimate.throughput_window_hours == 1


def test_stage_estimate_falls_back_to_the_six_hour_rate() -> None:
    estimate = StageEstimate.estimate(
        "conversion",
        queued=8,
        running=None,
        failed=None,
        completed_1h=1,
        completed_6h=24,
        completed_24h=96,
    )

    assert estimate.confidence == "medium"
    assert estimate.throughput_per_hour == 4
    assert estimate.throughput_window_hours == 6
    assert (estimate.lower_seconds, estimate.upper_seconds) == (4800, 10800)


def test_stage_estimate_reports_complete_only_when_no_failures_remain() -> None:
    complete = StageEstimate.estimate(
        "conversion",
        queued=0,
        running=0,
        failed=0,
        completed_1h=0,
        completed_6h=0,
        completed_24h=4,
    )
    blocked = StageEstimate.estimate(
        "conversion",
        queued=3,
        running=2,
        failed=1,
        completed_1h=10,
        completed_6h=20,
        completed_24h=4,
    )

    assert complete.eta_status == "complete"
    assert complete.confidence == "high"
    assert (complete.lower_seconds, complete.upper_seconds) == (0, 0)
    assert complete.progress_percent == 100
    assert complete.throughput_window_hours is None
    assert blocked.eta_status == "blocked"
    assert blocked.confidence == "unavailable"
    assert blocked.lower_seconds is blocked.upper_seconds is None
    assert blocked.progress_percent == 40


class ProcessingUser:
    """Return pre-shaped stage aggregates in query order."""

    def __init__(
        self,
        artifacts: ArtifactProcessingRecord,
        chunks: ChunkProcessingRecord,
    ) -> None:
        self.records = [[artifacts], [chunks]]

    @property
    def exec(self) -> ProcessingExec:
        return ProcessingExec(self.records)


class ProcessingExec:
    """Mimic the typed `user.exec[Record](statement)` boundary."""

    def __init__(
        self,
        records: list[list[ArtifactProcessingRecord] | list[ChunkProcessingRecord]],
    ) -> None:
        self.records = records

    def __getitem__[Record](self, model: type[Record]) -> ProcessingStatement[Record]:
        del model
        return ProcessingStatement(self.records)


class ProcessingStatement[Record]:
    """Return the next aggregate without opening a database transaction."""

    def __init__(
        self,
        records: list[list[ArtifactProcessingRecord] | list[ChunkProcessingRecord]],
    ) -> None:
        self.records = records

    async def __call__(self, statement: SelectBase) -> tuple[Record, ...]:
        del statement
        return cast("tuple[Record, ...]", tuple(self.records.pop(0)))


async def empty_recent(user: User) -> SimpleNamespace:
    del user
    return SimpleNamespace(artifacts=())


def processing_report(
    monkeypatch: pytest.MonkeyPatch,
    artifacts: ArtifactProcessingRecord,
    chunks: ChunkProcessingRecord,
) -> ProcessingReport:
    monkeypatch.setattr(operations.ArtifactDashboard, "load", empty_recent)
    return dbutil.run(ProcessingReport.load(cast("User", ProcessingUser(artifacts, chunks))))


def artifact_record(
    queued: int = 0,
    running: int = 0,
    failed: int = 0,
    completed_1h: int = 0,
    completed_6h: int = 0,
    completed_24h: int = 0,
) -> ArtifactProcessingRecord:
    return ArtifactProcessingRecord(
        queued=queued,
        running=running,
        failed=failed,
        completed_1h=completed_1h,
        completed_6h=completed_6h,
        completed_24h=completed_24h,
        oldest_at=None,
    )


def chunk_record(
    queued: int = 0,
    completed_1h: int = 0,
    completed_6h: int = 0,
    completed_24h: int = 0,
) -> ChunkProcessingRecord:
    return ChunkProcessingRecord(
        queued=queued,
        completed_1h=completed_1h,
        completed_6h=completed_6h,
        completed_24h=completed_24h,
    )


def test_processing_report_is_idle_only_after_backlog_and_failures_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    idle = processing_report(monkeypatch, artifact_record(), chunk_record())
    delayed = processing_report(
        monkeypatch,
        artifact_record(failed=1, completed_24h=2),
        chunk_record(),
    )

    assert idle.state == "idle"
    assert idle.recallable_lower_seconds == idle.enriched_lower_seconds == 0
    assert delayed.state == "delayed"
    assert delayed.recallable_lower_seconds is None
    assert delayed.enriched_lower_seconds is None


def test_processing_report_does_not_predict_unmaterialized_downstream_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = processing_report(
        monkeypatch,
        artifact_record(queued=2, completed_1h=20, completed_6h=20, completed_24h=20),
        chunk_record(queued=4, completed_1h=20, completed_6h=20, completed_24h=20),
    )

    assert report.state == "active"
    assert report.recallable_lower_seconds is not None
    assert report.enriched_lower_seconds is None
    assert report.enriched_upper_seconds is None


def test_processing_report_uses_projection_eta_after_conversion_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = processing_report(
        monkeypatch,
        artifact_record(completed_24h=3),
        chunk_record(queued=4, completed_1h=20, completed_6h=20, completed_24h=100),
    )
    projection = report.stages[1]

    assert report.state == "active"
    assert report.enriched_lower_seconds == projection.lower_seconds
    assert report.enriched_upper_seconds == projection.upper_seconds


def test_store_stage_metrics_expose_the_current_completion_window() -> None:
    now = datetime.now(UTC)
    thresholds = (now - timedelta(hours=1), now - timedelta(hours=6), now - timedelta(days=1))

    assert tuple(Artifact.Content.processing_counts(*thresholds).selected_columns.keys()) == (
        "queued",
        "running",
        "failed",
        "completed_1h",
        "completed_6h",
        "completed_24h",
        "oldest_at",
    )
    assert tuple(Chunk.processing_counts(*thresholds).selected_columns.keys()) == (
        "queued",
        "completed_1h",
        "completed_6h",
        "completed_24h",
    )


def test_processing_updates_emit_deterministic_reconnectable_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User.private(uuid5())
    first = ProcessingReport(generated_at=datetime.now(UTC), state="idle", stages=())
    second = first.model_copy(update={"state": "active"})
    reports = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(ProcessingReport, "load", reports)
    states = iter((False, False, True))
    pauses: list[float] = []

    async def disconnected() -> bool:
        return next(states)

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    async def collect() -> list[bytes]:
        return [
            event
            async for event in ProcessingUpdates(
                user,
                disconnected,
                pause,
                interval_seconds=2.5,
            ).events()
        ]

    events = dbutil.run(collect())

    assert [event.splitlines()[0] for event in events] == [b"id: 1", b"id: 2"]
    assert all(b"event: processing\nretry: 5000\n" in event for event in events)
    assert loads(events[0].split(b"data: ", 1)[1]) == first.model_dump(mode="json")
    assert events[0].split(b"data: ", 1)[1].startswith(b'{"enriched_lower_seconds"')
    assert pauses == [2.5, 2.5]
    assert [call.args for call in reports.await_args_list] == [(user,), (user,)]


def test_usage_report_reads_period_and_lifetime_from_the_durable_ledger(
    migrated_db: None,
) -> None:
    owner = uuid5()
    another_user = uuid5()
    now = datetime.now(UTC)
    recent = UsageCapture(
        capture_key="recent-owner-operation",
        occurred_at=now - timedelta(days=1),
        user_id=owner,
        operation=Usage.Event.Operation.recall,
        targets=(owner,),
        request_bytes=11,
        response_bytes=17,
        items=3,
        duration_ms=4.5,
    )
    old = recent.model_copy(
        update={
            "capture_key": "old-owner-operation",
            "occurred_at": now - timedelta(days=400),
            "operation": Usage.Event.Operation.share,
            "items": 2,
        }
    )
    invisible = recent.model_copy(
        update={
            "capture_key": "another-user-operation",
            "user_id": another_user,
            "targets": (another_user,),
        }
    )

    async def body() -> UsageReport:
        await dbutil.reset_db()
        job = UsageAccountingJob()
        await job.handle(recent)
        await job.handle(old)
        await job.handle(invisible)
        return await UsageReport.load(User.private(owner), 30)

    report = dbutil.run(body())

    assert report.summary.requests == 1
    assert report.summary.recalls == 1
    assert report.summary.shares == 0
    assert report.summary.items == 3
    assert report.summary.request_bytes == 11
    assert report.summary.response_bytes == 17
    assert report.lifetime.requests == 2
    assert report.lifetime.shares == 1
    assert len(report.points) == 1
    assert report.points[0].operation is Usage.Event.Operation.recall
    assert report.points[0].bucket.date() == recent.occurred_at.date()
