from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import asyncpg
import dbutil
import pytest
from factories import seed_artifact
from id_factory import uuid7

from aizk.background.jobs.conversion import DoclingConversionJob
from aizk.background.jobs.models import ArtifactConversionJob
from aizk.background.queue import Queue
from aizk.config import settings
from aizk.ops.doctor import (
    AizkDoctor,
    ConversionDiagnostics,
    ConversionQueueFailure,
    DoctorSummary,
    QueueDiagnostics,
    doctor,
    error_identity,
)
from aizk.store import Artifact
from aizk.store.identity import User

NOW = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)


def record(**values: str | bytes | int | datetime | None) -> asyncpg.Record:
    return cast("asyncpg.Record", values)


def test_error_identities_hide_messages_and_normalize_equivalent_whitespace() -> None:
    first = error_identity("ValueError", "private   source\ntext")
    second = error_identity("ValueError", "private source text")
    unsafe = error_identity("source text!", "another secret")
    visible = error_identity("ValueError", "unknown type 'private name'", True)

    assert first == second
    assert first is not None
    assert first.type == "ValueError"
    assert len(first.fingerprint) == 16
    assert "private" not in first.model_dump_json()
    assert unsafe is not None and unsafe.type == "UnknownError"
    assert visible is not None
    assert visible.sanitized_message == "unknown type '[redacted]'"
    assert error_identity(None, None) is None


@pytest.mark.parametrize(
    ("kind", "expected_guidance"),
    [
        ("stale_picked", "worker is gone"),
        ("long_running_picked", "downstream latency"),
    ],
)
def test_queue_issue_uses_safe_errors_and_kind_specific_guidance(
    kind: str,
    expected_guidance: str,
) -> None:
    diagnostics = QueueDiagnostics(
        timedelta(minutes=15),
        timedelta(hours=1),
        timedelta(hours=24),
        50,
        False,
    )
    issue = diagnostics.issue(
        record(
            id=7,
            entrypoint="work",
            kind=kind,
            updated_at=NOW,
            heartbeat_at=NOW,
            age_seconds=-1,
            attempts=3,
            exception_type="RuntimeError",
            exception_message="secret",
        )
    )

    assert issue.age_seconds == 0
    assert issue.latest_error is not None
    assert issue.latest_error.type == "RuntimeError"
    assert expected_guidance in issue.retry_guidance
    assert "secret" not in issue.model_dump_json()


def test_queue_failure_and_history_groups_are_aggregated_without_raw_messages() -> None:
    row = record(
        entrypoint=DoclingConversionJob.entrypoint,
        status="exception",
        exception_type="ValueError",
        exception_message="private chunk text",
        count=4,
        attempts_min=2,
        attempts_max=5,
        oldest_at=NOW - timedelta(hours=1),
        newest_at=NOW,
    )

    diagnostics = QueueDiagnostics(
        timedelta(minutes=15),
        timedelta(hours=1),
        timedelta(hours=24),
        50,
        False,
    )
    current = diagnostics.failure_group(row)
    history = diagnostics.exception_group(row)

    assert current.count == history.count == 4
    assert current.attempts_min == 2 and current.attempts_max == 5
    assert "admin queue retry conversion" in current.remediation
    assert "private chunk text" not in current.model_dump_json()
    assert "private chunk text" not in history.model_dump_json()


def test_conversion_failure_index_skips_invalid_queue_payloads() -> None:
    content_id = uuid7()
    payload = ArtifactConversionJob(
        artifact_content_id=content_id,
        scopes=frozenset({settings.system_user_id}),
    ).encode()
    diagnostics = QueueDiagnostics(
        timedelta(minutes=15),
        timedelta(hours=1),
        timedelta(hours=24),
        50,
        False,
    )
    failures = diagnostics.conversion_failures(
        [
            record(
                id=1,
                payload=payload,
                attempts=5,
                exception_type="ValueError",
                exception_message="secret",
            ),
            record(
                id=2,
                payload=b"not-json",
                attempts=1,
                exception_type=None,
                exception_message=None,
            ),
        ]
    )

    assert set(failures) == {UUID(str(content_id))}
    assert failures[UUID(str(content_id))].attempts == 5
    assert "secret" not in failures[UUID(str(content_id))].model_dump_json()


def test_conversion_job_index_separates_terminal_and_active_jobs() -> None:
    failed_id, queued_id, picked_id = uuid7(), uuid7(), uuid7()
    diagnostics = QueueDiagnostics(
        timedelta(minutes=15),
        timedelta(hours=1),
        timedelta(hours=24),
        50,
        False,
    )
    rows = [
        record(
            id=index,
            payload=ArtifactConversionJob(
                artifact_content_id=content_id,
                scopes=frozenset({settings.system_user_id}),
            ).encode(),
            status=status,
            attempts=index,
            exception_type="ValueError" if status == "failed" else None,
            exception_message="secret" if status == "failed" else None,
        )
        for index, (content_id, status) in enumerate(
            ((failed_id, "failed"), (queued_id, "queued"), (picked_id, "picked")),
            start=1,
        )
    ]
    rows.append(
        record(
            id=4,
            payload=b"invalid",
            status="queued",
            attempts=0,
            exception_type=None,
            exception_message=None,
        )
    )

    failures, activity = diagnostics.conversion_jobs(rows)

    assert set(failures) == {UUID(str(failed_id))}
    assert activity == {
        UUID(str(queued_id)): "queued",
        UUID(str(picked_id)): "picked",
    }


def test_doctor_rejects_invalid_windows_and_limits() -> None:
    with pytest.raises(ValueError, match="windows"):
        AizkDoctor(timedelta(0), timedelta(minutes=1), timedelta(hours=1), 1)
    with pytest.raises(ValueError, match="limit"):
        AizkDoctor(
            timedelta(minutes=1),
            timedelta(minutes=2),
            timedelta(hours=1),
            0,
        )


def test_findings_separate_current_blockers_from_recent_history() -> None:
    findings = AizkDoctor.findings(
        DoctorSummary(
            current_failed_jobs=1,
            stale_picked_jobs=2,
            long_running_picked_jobs=3,
            recent_exception_events=4,
            failed_conversions=5,
            orphaned_active_conversions=6,
            stale_active_conversions=7,
        )
    )

    assert {finding.code for finding in findings} == {
        "queue_failed",
        "queue_stale",
        "queue_long_running",
        "conversion_failed",
        "conversion_terminal_queue",
        "conversion_stale",
        "recent_exception_history",
    }
    assert (
        next(item for item in findings if item.code == "recent_exception_history").severity
        == "info"
    )
    assert AizkDoctor.findings(DoctorSummary()) == ()


def test_doctor_correlates_terminal_queue_jobs_with_durable_conversion_state(
    migrated_db: None,
) -> None:
    async def run() -> None:
        await dbutil.reset_db()
        owner = settings.system_user_id
        terminal = await seed_artifact(
            owner,
            [owner],
            name="terminal.pdf",
            state=Artifact.Content.State.processing,
        )
        stale = await seed_artifact(
            owner,
            [owner],
            name="stale.pdf",
            state=Artifact.Content.State.processing,
        )
        fresh = await seed_artifact(
            owner,
            [owner],
            name="fresh.pdf",
            state=Artifact.Content.State.processing,
        )
        queued = await seed_artifact(
            owner,
            [owner],
            name="queued.pdf",
            state=Artifact.Content.State.failed,
        )
        picked = await seed_artifact(
            owner,
            [owner],
            name="picked.pdf",
            state=Artifact.Content.State.failed,
        )
        failed = await seed_artifact(
            owner,
            [owner],
            name="failed.pdf",
            state=Artifact.Content.State.failed,
        )
        async with User.system().owner as session:
            stale_row = await session.get(Artifact.Content, stale.content.id)
            fresh_row = await session.get(Artifact.Content, fresh.content.id)
            queued_row = await session.get(Artifact.Content, queued.content.id)
            picked_row = await session.get(Artifact.Content, picked.content.id)
            failed_row = await session.get(Artifact.Content, failed.content.id)
            assert (
                stale_row is not None
                and fresh_row is not None
                and queued_row is not None
                and picked_row is not None
                and failed_row is not None
            )
            stale_row.updated_at = NOW - timedelta(hours=2)
            fresh_row.updated_at = NOW - timedelta(minutes=2)
            queued_row.updated_at = NOW - timedelta(hours=2)
            picked_row.updated_at = NOW - timedelta(hours=2)
            queued_row.error = "old queued failure"
            picked_row.error = "old picked failure"
            failed_row.error = "private conversion output"

        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            names = queue.queries.qbe.settings
            await queue.connection.execute(f"DELETE FROM {names.queue_table_log}")
            await queue.connection.execute(f"DELETE FROM {names.queue_table}")
            terminal_job = await queue.connection.fetchval(
                f"""
                INSERT INTO {names.queue_table}
                    (
                        priority,
                        status,
                        entrypoint,
                        dedupe_key,
                        payload,
                        attempts,
                        updated,
                        heartbeat
                    )
                VALUES (75, 'failed', $1, $2, $3, 5, $4, $4)
                RETURNING id
                """,
                DoclingConversionJob.entrypoint,
                str(terminal.content.id),
                ArtifactConversionJob(
                    artifact_content_id=terminal.content.id,
                    scopes=frozenset({owner}),
                ).encode(),
                NOW - timedelta(minutes=20),
            )
            stale_job = await queue.connection.fetchval(
                f"""
                INSERT INTO {names.queue_table}
                    (priority, status, entrypoint, attempts, updated, heartbeat)
                VALUES (50, 'picked', 'stale_work', 2, $1, $1)
                RETURNING id
                """,
                NOW - timedelta(hours=2),
            )
            await queue.connection.execute(
                f"""
                INSERT INTO {names.queue_table}
                    (priority, status, entrypoint, attempts, updated, heartbeat)
                VALUES (50, 'picked', 'long_work', 1, $1, $2)
                """,
                NOW - timedelta(hours=2),
                NOW - timedelta(minutes=1),
            )
            await queue.connection.execute(
                f"""
                INSERT INTO {names.queue_table}
                    (
                        priority,
                        status,
                        entrypoint,
                        dedupe_key,
                        payload,
                        attempts,
                        updated,
                        heartbeat
                    )
                VALUES
                    (75, 'queued', $1, $2, $3, 0, $6, $6),
                    (75, 'picked', $1, $4, $5, 1, $6, $7)
                """,
                DoclingConversionJob.entrypoint,
                str(queued.content.id),
                ArtifactConversionJob(
                    artifact_content_id=queued.content.id,
                    scopes=frozenset({owner}),
                ).encode(),
                str(picked.content.id),
                ArtifactConversionJob(
                    artifact_content_id=picked.content.id,
                    scopes=frozenset({owner}),
                ).encode(),
                NOW - timedelta(hours=2),
                NOW - timedelta(minutes=1),
            )
            traceback = (
                '{"job_id": 1, "timestamp": "2026-07-20T00:00:00Z",'
                ' "exception_type": "ValueError",'
                ' "exception_message": "private source excerpt", "traceback": "private",'
                ' "additional_context": null}'
            )
            await queue.connection.execute(
                f"""
                INSERT INTO {names.queue_table_log}
                    (job_id, status, priority, entrypoint, traceback, created)
                VALUES
                    ($1, 'exception', 75, $2, CAST($3 AS jsonb), $4),
                    ($1, 'failed', 75, $2, NULL, $5),
                    ($6, 'exception', 50, 'stale_work', CAST($3 AS jsonb), $4)
                """,
                terminal_job,
                DoclingConversionJob.entrypoint,
                traceback,
                NOW - timedelta(minutes=25),
                NOW - timedelta(minutes=20),
                stale_job,
            )

        report = await AizkDoctor(
            timedelta(minutes=15),
            timedelta(hours=1),
            timedelta(hours=24),
            50,
        ).diagnose(NOW)

        assert report.healthy is False
        assert report.summary.current_failed_jobs == 1
        assert report.summary.stale_picked_jobs == 1
        assert report.summary.long_running_picked_jobs == 2
        assert report.summary.orphaned_active_conversions == 1
        assert report.summary.queued_active_conversions == 1
        assert report.summary.stale_active_conversions == 1
        assert report.summary.fresh_active_conversions == 2
        assert report.summary.failed_conversions == 1
        assert {issue.kind for issue in report.queue_issues} == {
            "stale_picked",
            "long_running_picked",
        }
        stale_issue = next(item for item in report.queue_issues if item.kind == "stale_picked")
        assert stale_issue.age_seconds == 7200
        terminal_conversion = next(
            item for item in report.conversions if item.content_id == terminal.content.id
        )
        assert terminal_conversion.state == "active_terminal_queue"
        assert terminal_conversion.queue_failure is not None
        assert terminal_conversion.queue_failure.attempts == 5
        assert terminal_conversion.artifact_name == "terminal.pdf"
        queued_conversion = next(
            item for item in report.conversions if item.content_id == queued.content.id
        )
        picked_conversion = next(
            item for item in report.conversions if item.content_id == picked.content.id
        )
        assert queued_conversion.state == "active_queued"
        assert queued_conversion.queue_status == "queued"
        assert "No retry" in queued_conversion.retry_guidance
        assert picked_conversion.state == "active_fresh"
        assert picked_conversion.queue_status == "picked"
        assert "No retry" in picked_conversion.retry_guidance
        serialized = report.model_dump_json()
        assert "private source excerpt" not in serialized
        assert "private conversion output" not in serialized
        assert report.queue_failure_groups[0].count == 1
        assert report.recent_exception_groups

        await dbutil.reset_db()
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            names = queue.queries.qbe.settings
            await queue.connection.execute(f"DELETE FROM {names.queue_table_log}")
            await queue.connection.execute(f"DELETE FROM {names.queue_table}")
        healthy = await doctor(
            stale_minutes=15,
            long_running_minutes=60,
            history_hours=24,
            limit=50,
            show_error_messages=True,
        )
        assert healthy.healthy is True
        assert healthy.error_messages_included is True
        assert healthy.findings == ()

    dbutil.run(run())


def test_conversion_diagnostic_classifies_all_durable_states() -> None:
    owner = settings.system_user_id
    content = Artifact.Content(
        artifact_id=uuid7(),
        blob_id=uuid7(),
        revision=1,
        created_by=owner,
        scopes=[owner],
        updated_at=NOW,
    )
    diagnostics = ConversionDiagnostics(timedelta(hours=1), 10, False)
    queue_failure = ConversionQueueFailure(job_id=1, attempts=5, error=None)

    fresh = diagnostics.diagnostic(content, "note", NOW, NOW - timedelta(hours=1), None)
    stale = diagnostics.diagnostic(
        content.model_copy(update={"updated_at": NOW - timedelta(hours=2)}),
        "note",
        NOW,
        NOW - timedelta(hours=1),
        None,
    )
    terminal = diagnostics.diagnostic(
        content,
        "note",
        NOW,
        NOW - timedelta(hours=1),
        queue_failure,
    )
    queued = diagnostics.diagnostic(
        content,
        "note",
        NOW,
        NOW - timedelta(hours=1),
        None,
        "queued",
    )
    picked = diagnostics.diagnostic(
        content.model_copy(update={"updated_at": NOW - timedelta(hours=2)}),
        "note",
        NOW,
        NOW - timedelta(hours=1),
        None,
        "picked",
    )
    failed = diagnostics.diagnostic(
        content.model_copy(
            update={
                "state": Artifact.Content.State.failed,
                "error": "secret",
                "updated_at": NOW + timedelta(minutes=1),
            }
        ),
        "note",
        NOW,
        NOW - timedelta(hours=1),
        queue_failure,
    )
    failed_queued = diagnostics.diagnostic(
        content.model_copy(update={"state": Artifact.Content.State.failed}),
        "note",
        NOW,
        NOW - timedelta(hours=1),
        None,
        "queued",
    )
    failed_picked = diagnostics.diagnostic(
        content.model_copy(update={"state": Artifact.Content.State.failed}),
        "note",
        NOW,
        NOW - timedelta(hours=1),
        None,
        "picked",
    )

    assert [
        fresh.state,
        stale.state,
        terminal.state,
        queued.state,
        picked.state,
        failed.state,
        failed_queued.state,
        failed_picked.state,
    ] == [
        "active_fresh",
        "active_stale",
        "active_terminal_queue",
        "active_queued",
        "active_fresh",
        "failed",
        "active_queued",
        "active_fresh",
    ]
    assert terminal.queue_status == "failed"
    assert queued.queue_status == "queued"
    assert picked.queue_status == "picked"
    assert "No retry" in failed_queued.retry_guidance
    assert "No retry" in failed_picked.retry_guidance
    assert failed.age_seconds == 0
    assert failed.error is not None
    assert "secret" not in failed.model_dump_json()
