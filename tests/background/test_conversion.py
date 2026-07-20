import asyncio
from types import TracebackType
from typing import Self, cast

import dbutil
import pytest
from factories import seed_artifact
from id_factory import uuid5, uuid7
from pydantic import UUID7

import aizk.background.jobs.conversion as conversion_module
from aizk.background.jobs.conversion import (
    ArtifactQueue,
    ArtifactRecovery,
    DoclingConversionJob,
    retry_failed_artifacts,
)
from aizk.background.jobs.models import ArtifactConversionJob
from aizk.background.queue import Queue as ProductionQueue
from aizk.config import settings
from aizk.store import Artifact
from aizk.store.identity import User
from aizk.types import Scopes


class Processor:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID7, Scopes]] = []

    async def process(self, content_id: UUID7, scopes: Scopes) -> None:
        self.calls.append((content_id, scopes))


class FakeQueue:
    def __init__(self, admitted: bool) -> None:
        self.admitted = admitted
        self.enqueued: list[tuple[ArtifactConversionJob, str]] = []
        self.requeue_limits: list[int] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    async def enqueue(
        self,
        job: DoclingConversionJob,
        payload: ArtifactConversionJob,
        dedupe_key: str,
    ) -> bool:
        del job
        self.enqueued.append((payload, dedupe_key))
        return self.admitted

    async def requeue_failed(self, job: type[DoclingConversionJob], limit: int = 100) -> int:
        assert job is DoclingConversionJob
        self.requeue_limits.append(limit)
        return 1


def test_conversion_job_delegates_the_durable_original_to_the_processor() -> None:
    processor = Processor()
    payload = ArtifactConversionJob(
        artifact_content_id=uuid7(),
        scopes=frozenset({uuid5()}),
    )
    job = DoclingConversionJob(processor)

    asyncio.run(job.handle(payload))

    assert processor.calls == [(payload.artifact_content_id, payload.scopes)]
    assert job.entrypoint == "aizk_convert_artifact"
    assert job.priority == 75
    assert job.concurrency_limit == settings.docling_concurrency


@pytest.mark.parametrize("admitted", [True, False])
def test_artifact_queue_enqueues_only_ids_and_recovers_a_held_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    admitted: bool,
) -> None:
    connection = FakeQueue(admitted)
    monkeypatch.setattr(conversion_module, "Queue", lambda dsn: connection)
    content_id, scopes = uuid7(), frozenset({uuid5()})

    result = asyncio.run(
        ArtifactQueue(DoclingConversionJob(Processor())).enqueue(content_id, scopes)
    )

    assert result is admitted
    [(payload, dedupe_key)] = connection.enqueued
    assert payload == ArtifactConversionJob(artifact_content_id=content_id, scopes=scopes)
    assert dedupe_key == str(content_id)
    assert connection.requeue_limits == ([100] if not admitted else [])


def test_failed_artifact_retry_is_bounded_and_conversion_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeQueue(admitted=True)
    orphan_limits: list[int] = []

    async def enqueue_orphans(
        recovery: ArtifactRecovery,
        queue: ProductionQueue,
        limit: int,
    ) -> int:
        del recovery, queue
        orphan_limits.append(limit)
        return 2

    monkeypatch.setattr(conversion_module, "Queue", lambda dsn: connection)
    monkeypatch.setattr(ArtifactRecovery, "enqueue_orphans", enqueue_orphans)

    count = asyncio.run(retry_failed_artifacts(limit=11))

    assert count == 3
    assert connection.requeue_limits == [11]
    assert orphan_limits == [10]


def test_artifact_recovery_rejects_invalid_limits_and_stops_at_the_budget() -> None:
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(ArtifactRecovery().retry(0))
    assert (
        asyncio.run(
            ArtifactRecovery().enqueue_orphans(
                cast(ProductionQueue, FakeQueue(admitted=True)),
                0,
            )
        )
        == 0
    )


def test_artifact_recovery_enqueues_only_orphaned_durable_failures(
    migrated_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        await dbutil.reset_db()
        owner = settings.system_user_id
        retained = await seed_artifact(
            owner,
            [owner],
            name="retained.pdf",
            state=Artifact.Content.State.processing,
        )
        first = await seed_artifact(
            owner,
            [owner],
            name="first.pdf",
            state=Artifact.Content.State.failed,
        )
        second = await seed_artifact(
            owner,
            [owner],
            name="second.pdf",
            state=Artifact.Content.State.failed,
        )
        protected = await seed_artifact(
            owner,
            [owner],
            name="protected.pdf",
            state=Artifact.Content.State.failed,
        )
        async with User.system().owner as session:
            for stored in (first, second, protected):
                row = await session.get(Artifact.Content, stored.content.id)
                assert row is not None
                row.error = "old failure"

        async with ProductionQueue(dsn=settings.asyncpg_dsn) as queue:
            names = queue.queries.qbe.settings
            await queue.connection.execute(f"DELETE FROM {names.queue_table_log}")
            await queue.connection.execute(f"DELETE FROM {names.queue_table}")
            await queue.connection.execute(
                f"""
                INSERT INTO {names.queue_table}
                    (priority, status, entrypoint, dedupe_key, payload, attempts)
                VALUES
                    (75, 'failed', $1, $2, $3, 5),
                    (75, 'queued', $1, $4, $5, 0),
                    (75, 'queued', $1, 'invalid-payload', 'invalid', 0)
                """,
                DoclingConversionJob.entrypoint,
                str(retained.content.id),
                ArtifactConversionJob(
                    artifact_content_id=retained.content.id,
                    scopes=frozenset({owner}),
                ).encode(),
                str(protected.content.id),
                ArtifactConversionJob(
                    artifact_content_id=protected.content.id,
                    scopes=frozenset({owner}),
                ).encode(),
            )

        assert await retry_failed_artifacts(limit=3) == 3

        async with User.system().owner as session:
            rows = {
                content_id: await session.get(Artifact.Content, content_id)
                for content_id in (
                    retained.content.id,
                    first.content.id,
                    second.content.id,
                    protected.content.id,
                )
            }
            assert rows[retained.content.id] is not None
            assert rows[retained.content.id].state == Artifact.Content.State.processing
            for stored in (first, second):
                row = rows[stored.content.id]
                assert row is not None
                assert row.state == Artifact.Content.State.queued
                assert row.error is None
                assert row.processed_at is None
            protected_row = rows[protected.content.id]
            assert protected_row is not None
            assert protected_row.state == Artifact.Content.State.failed
            assert protected_row.error == "old failure"

        async with ProductionQueue(dsn=settings.asyncpg_dsn) as queue:
            names = queue.queries.qbe.settings
            active = await queue.connection.fetch(
                f"""
                SELECT dedupe_key, status::text AS status
                FROM {names.queue_table}
                WHERE entrypoint = $1 AND dedupe_key IS NOT NULL
                ORDER BY dedupe_key
                """,
                DoclingConversionJob.entrypoint,
            )
        states = {row["dedupe_key"]: row["status"] for row in active}
        assert states[str(retained.content.id)] == "queued"
        assert states[str(first.content.id)] == "queued"
        assert states[str(second.content.id)] == "queued"
        assert states[str(protected.content.id)] == "queued"
        assert await retry_failed_artifacts(limit=3) == 0

        async def no_active_ids(
            recovery: ArtifactRecovery,
            queue: ProductionQueue,
        ) -> tuple[UUID7, ...]:
            del recovery, queue
            return ()

        monkeypatch.setattr(ArtifactRecovery, "active_content_ids", no_active_ids)
        racing_queue = FakeQueue(admitted=False)
        assert (
            await ArtifactRecovery().enqueue_orphans(
                cast(ProductionQueue, racing_queue),
                1,
            )
            == 0
        )
        assert racing_queue.enqueued[0][0].artifact_content_id == protected.content.id
        async with User.system().owner as session:
            protected_row = await session.get(Artifact.Content, protected.content.id)
            assert protected_row is not None
            assert protected_row.state == Artifact.Content.State.failed

    dbutil.run(run())
