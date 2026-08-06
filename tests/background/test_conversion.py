import asyncio
from datetime import UTC, datetime
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
    ArtifactRechunk,
    ArtifactReconversion,
    ArtifactRecovery,
    DoclingConversionJob,
    MarkdownReindexJob,
    ReconversionSweep,
    rechunk_artifacts,
    reconvert_scanned_documents,
    reconvert_web_pages,
    retry_failed_artifacts,
)
from aizk.background.jobs.models import ArtifactConversionJob, ArtifactReindexJob
from aizk.background.queue import Queue as ProductionQueue
from aizk.config import settings
from aizk.store import Artifact
from aizk.store.identity import User
from aizk.types import Scopes

READY = Artifact.Content.State.ready


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


def test_web_page_reconversion_requeues_converted_pages_and_nothing_else(
    migrated_db: None,
) -> None:
    async def run() -> None:
        await dbutil.reset_db()
        owner = settings.system_user_id
        page = await seed_artifact(
            owner,
            [owner],
            name="datahub",
            media_type="text/html",
            source_uri="https://github.com/datahub-project/datahub",
            state=Artifact.Content.State.ready,
        )
        paper = await seed_artifact(
            owner,
            [owner],
            name="paper.pdf",
            media_type="application/pdf",
            source_uri="https://files.example/paper.pdf",
            state=Artifact.Content.State.ready,
        )
        uploaded = await seed_artifact(
            owner,
            [owner],
            name="page.html",
            media_type="text/html",
            state=Artifact.Content.State.ready,
        )
        converting = await seed_artifact(
            owner,
            [owner],
            name="pending.html",
            media_type="text/html",
            source_uri="https://example.org/pending",
            state=Artifact.Content.State.processing,
        )

        async with ProductionQueue(dsn=settings.asyncpg_dsn) as queue:
            names = queue.queries.qbe.settings
            await queue.connection.execute(f"DELETE FROM {names.queue_table_log}")
            await queue.connection.execute(f"DELETE FROM {names.queue_table}")

        assert await reconvert_web_pages(limit=10) == 1
        assert await reconvert_web_pages(limit=10) == 0

        # A page still held by the queue is claimed again and its enqueue refused, which is not
        # counted as new work while the state stays honest about the task that already exists.
        async with User.system().owner as session:
            held = await session.get(Artifact.Content, page.content.id)
            assert held is not None
            held.state = Artifact.Content.State.ready
        assert await reconvert_web_pages(limit=10) == 0

        async with User.system().owner as session:
            states = {
                stored.content.id: await session.get(Artifact.Content, stored.content.id)
                for stored in (page, paper, uploaded, converting)
            }
        assert states[page.content.id] is not None
        assert states[page.content.id].state == Artifact.Content.State.queued
        for untouched, expected in (
            (paper, Artifact.Content.State.ready),
            (uploaded, Artifact.Content.State.ready),
            (converting, Artifact.Content.State.processing),
        ):
            row = states[untouched.content.id]
            assert row is not None and row.state == expected

    dbutil.run(run())


def test_reconversion_rejects_an_empty_budget() -> None:
    sweep = ReconversionSweep(media_prefixes=("application/pdf",))
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(ArtifactReconversion(sweep).enqueue(0))


def test_a_worker_finishing_mid_sweep_keeps_its_ready_state(
    migrated_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep commits `queued` before a task exists, so a fast worker is never overwritten."""

    async def run() -> None:
        await dbutil.reset_db()
        owner = settings.system_user_id
        first = await seed_artifact(
            owner,
            [owner],
            name="first",
            media_type="text/html",
            source_uri="https://example.org/first",
            state=Artifact.Content.State.ready,
        )
        second = await seed_artifact(
            owner,
            [owner],
            name="second",
            media_type="text/html",
            source_uri="https://example.org/second",
            state=Artifact.Content.State.ready,
        )

        class CompletingQueue(FakeQueue):
            """Run the whole worker lifecycle inside the enqueue that hands out the task."""

            async def enqueue(
                self,
                job: DoclingConversionJob,
                payload: ArtifactConversionJob,
                dedupe_key: str,
            ) -> bool:
                admitted = await super().enqueue(job, payload, dedupe_key)
                if payload.artifact_content_id == first.content.id:
                    async with User.system().owner as session:
                        row = await session.get(Artifact.Content, payload.artifact_content_id)
                        assert row is not None
                        assert row.state == Artifact.Content.State.queued
                        row.state = Artifact.Content.State.ready
                return admitted

        monkeypatch.setattr(conversion_module, "Queue", lambda dsn: CompletingQueue(admitted=True))

        assert await reconvert_web_pages(limit=10) == 2

        async with User.system().owner as session:
            completed = await session.get(Artifact.Content, first.content.id)
            queued = await session.get(Artifact.Content, second.content.id)
        assert completed is not None and completed.state == Artifact.Content.State.ready
        assert queued is not None and queued.state == Artifact.Content.State.queued

    dbutil.run(run())


def test_scanned_reconversion_requeues_everything_ocr_read_whatever_its_source(
    migrated_db: None,
) -> None:
    async def run() -> None:
        await dbutil.reset_db()
        owner = settings.system_user_id
        scan = await seed_artifact(
            owner,
            [owner],
            name="scan.pdf",
            media_type="application/pdf",
            state=Artifact.Content.State.ready,
        )
        photo = await seed_artifact(
            owner,
            [owner],
            name="whiteboard.png",
            media_type="image/png",
            source_uri="https://files.example/whiteboard.png",
            state=Artifact.Content.State.ready,
        )
        page = await seed_artifact(
            owner,
            [owner],
            name="page.html",
            media_type="text/html",
            source_uri="https://example.org/page",
            state=Artifact.Content.State.ready,
        )

        async with ProductionQueue(dsn=settings.asyncpg_dsn) as queue:
            names = queue.queries.qbe.settings
            await queue.connection.execute(f"DELETE FROM {names.queue_table_log}")
            await queue.connection.execute(f"DELETE FROM {names.queue_table}")

        assert await reconvert_scanned_documents(limit=10) == 2

        async with User.system().owner as session:
            states = {
                stored.content.id: await session.get(Artifact.Content, stored.content.id)
                for stored in (scan, photo, page)
            }
        for requeued in (scan, photo):
            row = states[requeued.content.id]
            assert row is not None and row.state == Artifact.Content.State.queued
        untouched = states[page.content.id]
        assert untouched is not None and untouched.state == Artifact.Content.State.ready

    dbutil.run(run())


def test_reindex_job_delegates_the_stored_markdown_to_the_reindexer() -> None:
    class Reindexer:
        def __init__(self) -> None:
            self.calls: list[tuple[UUID7, Scopes]] = []

        async def reindex(self, content_id: UUID7, scopes: Scopes) -> None:
            self.calls.append((content_id, scopes))

    reindexer = Reindexer()
    payload = ArtifactReindexJob(artifact_content_id=uuid7(), scopes=frozenset({uuid5()}))
    job = MarkdownReindexJob(reindexer)

    asyncio.run(job.handle(payload))

    assert reindexer.calls == [(payload.artifact_content_id, payload.scopes)]
    assert job.entrypoint == "aizk_reindex_artifact"
    assert job.priority == 75
    assert job.concurrency_limit == settings.docling_concurrency


def test_rechunk_rejects_an_empty_budget() -> None:
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(ArtifactRechunk().enqueue(0))


def test_rechunk_sweeps_converted_originals_least_recently_indexed_first(
    migrated_db: None,
) -> None:
    """`indexed_at` is the cursor, so a repeated pass walks forward instead of the same head."""

    async def run() -> None:
        await dbutil.reset_db()
        owner = settings.system_user_id
        never = await seed_artifact(owner, [owner], name="never.pdf", state=READY)
        stale = await seed_artifact(owner, [owner], name="stale.pdf", state=READY)
        fresh = await seed_artifact(owner, [owner], name="fresh.pdf", state=READY)
        textless = await seed_artifact(owner, [owner], name="textless.pdf", state=READY)
        converting = await seed_artifact(
            owner,
            [owner],
            name="converting.pdf",
            state=Artifact.Content.State.processing,
        )
        stamps = {
            never.content.id: None,
            stale.content.id: datetime(2026, 1, 1, tzinfo=UTC),
            fresh.content.id: datetime(2026, 7, 1, tzinfo=UTC),
            converting.content.id: None,
        }
        async with User.system().owner as session:
            for content_id, indexed_at in stamps.items():
                row = await session.get(Artifact.Content, content_id)
                assert row is not None
                row.markdown = "# Converted\n"
                row.indexed_at = indexed_at

        assert await ArtifactRechunk().converted(limit=10) == (
            (never.content.id, frozenset({owner})),
            (stale.content.id, frozenset({owner})),
            (fresh.content.id, frozenset({owner})),
        )
        assert textless.content.id not in {row for row, _ in await ArtifactRechunk().converted(10)}

        async with ProductionQueue(dsn=settings.asyncpg_dsn) as queue:
            names = queue.queries.qbe.settings
            await queue.connection.execute(f"DELETE FROM {names.queue_table_log}")
            await queue.connection.execute(f"DELETE FROM {names.queue_table}")

        assert await rechunk_artifacts(limit=2) == 2
        # The two still queued are refused by deduplication rather than queued twice.
        assert await rechunk_artifacts(limit=2) == 0

        async with ProductionQueue(dsn=settings.asyncpg_dsn) as queue:
            names = queue.queries.qbe.settings
            queued = await queue.connection.fetch(
                f"SELECT dedupe_key FROM {names.queue_table} WHERE entrypoint = $1",
                MarkdownReindexJob.entrypoint,
            )
        assert {row["dedupe_key"] for row in queued} == {
            str(never.content.id),
            str(stale.content.id),
        }

        async with User.system().owner as session:
            untouched = await session.get(Artifact.Content, never.content.id)
            assert untouched is not None
            # The conversion is still current, so the sweep never disturbs the workflow state.
            assert untouched.state == READY

    dbutil.run(run())
