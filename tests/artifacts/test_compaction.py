import asyncio
from datetime import UTC, datetime, timedelta
from random import Random
from typing import cast

import pytest
from id_factory import uuid7
from obstore.store import MemoryStore
from patos import sql
from pydantic import UUID7

from aizk.artifacts import ArtifactCompaction, ArtifactRepository, CompactionDisabled
from aizk.storage import ByteStore, StoredBytes, StoredObject
from aizk.store import Blob


class Recorder:
    """Stand in for the PostgreSQL side of one compaction pass."""

    def __init__(self, candidates: tuple[StoredObject, ...] = ()) -> None:
        self.candidates = candidates
        self.asked: list[tuple[int, int]] = []
        self.recorded: list[tuple[UUID7, int, StoredBytes | None]] = []

    async def compaction_candidates(self, level: int, limit: int) -> tuple[StoredObject, ...]:
        self.asked.append((level, limit))
        return self.candidates[:limit]

    async def record_compaction(
        self,
        blob_id: UUID7,
        level: int,
        verified_at: datetime,
        replacement: StoredBytes | None = None,
    ) -> None:
        assert verified_at.tzinfo is UTC
        self.recorded.append((blob_id, level, replacement))


def store(backend: MemoryStore, compression_enabled: bool = True) -> ByteStore:
    return ByteStore(
        backend=backend,
        upload_byte_limit=1 << 20,
        internal_download_lifetime=timedelta(minutes=5),
        compression_enabled=compression_enabled,
    )


async def seed(backend: MemoryStore, key: str, data: bytes) -> StoredObject:
    """Write one object verbatim, the way a store with no compression policy left it."""
    await backend.put_async(key, data, mode="create")
    return StoredObject(
        id=uuid7(),
        key=key,
        content_hash=sql.uuid8(data),
        size=len(data),
        stored_size=len(data),
        encoding=Blob.Encoding.identity,
    )


def test_a_legacy_object_moves_to_a_denser_layout_without_changing_one_byte() -> None:
    async def body() -> None:
        backend = MemoryStore()
        payload = b"a preserved contract paragraph. " * 200
        legacy = await seed(backend, "objects/legacy", payload)
        recorder = Recorder((legacy,))
        byte_store = store(backend)

        report = await ArtifactCompaction(byte_store, cast(ArtifactRepository, recorder)).compact(
            limit=10
        )

        assert recorder.asked == [(9, 10)]
        assert report.examined == 1
        assert report.rewritten == 1
        assert report.failed == 0
        assert report.stored_bytes_before == len(payload)
        assert report.stored_bytes_after < report.stored_bytes_before
        assert report.reclaimed == report.stored_bytes_before - report.stored_bytes_after

        blob_id, level, replacement = recorder.recorded[0]
        assert (blob_id, level) == (legacy.id, 9)
        assert replacement is not None
        assert replacement.encoding is Blob.Encoding.zstd
        assert replacement.encoding_level == 9
        assert replacement.key != legacy.key
        # The identity of the artifact is untouched, only where and how it sits changed.
        assert replacement.content_hash == legacy.content_hash
        assert replacement.size == legacy.size
        # The moved object still restores to exactly the original bytes.
        assert (
            await byte_store.get(
                replacement.key,
                encoding=replacement.encoding,
                expected_size=replacement.size,
                expected_hash=replacement.content_hash,
            )
            == payload
        )
        # The old key is only dropped after PostgreSQL points at the new one.
        with pytest.raises(FileNotFoundError):
            await backend.get_async(legacy.key)

    asyncio.run(body())


def test_an_incompressible_object_is_stamped_and_left_exactly_where_it_is() -> None:
    async def body() -> None:
        backend = MemoryStore()
        payload = Random(7).randbytes(4096)
        legacy = await seed(backend, "objects/dense", payload)
        recorder = Recorder((legacy,))
        byte_store = store(backend)

        report = await ArtifactCompaction(byte_store, cast(ArtifactRepository, recorder)).compact(
            limit=10
        )

        assert (report.examined, report.rewritten, report.failed) == (1, 0, 0)
        assert report.reclaimed == 0
        # Stamped so the next pass skips it, with no replacement recorded.
        assert recorder.recorded == [(legacy.id, 9, None)]
        # The original is untouched and the speculative rewrite left nothing behind.
        assert bytes(await (await backend.get_async(legacy.key)).bytes_async()) == payload
        keys = [entry["path"] async for batch in backend.list() for entry in batch]
        assert keys == [legacy.key]

    asyncio.run(body())


def test_an_unreadable_object_is_reported_and_stays_a_candidate() -> None:
    async def body() -> None:
        backend = MemoryStore()
        missing = StoredObject(
            id=uuid7(),
            key="objects/gone",
            content_hash=sql.uuid8(b"gone"),
            size=4,
            stored_size=4,
            encoding=Blob.Encoding.identity,
        )
        recorder = Recorder((missing,))

        report = await ArtifactCompaction(
            store(backend), cast(ArtifactRepository, recorder)
        ).compact(limit=10)

        assert (report.examined, report.rewritten, report.failed) == (1, 0, 1)
        assert report.stored_bytes_after == report.stored_bytes_before
        # Nothing is stamped, so the object is retried and the integrity pass still sees it.
        assert recorder.recorded == []

    asyncio.run(body())


def test_compaction_refuses_to_run_while_compression_is_turned_off() -> None:
    async def body() -> None:
        recorder = Recorder()
        compaction = ArtifactCompaction(
            store(MemoryStore(), compression_enabled=False),
            cast(ArtifactRepository, recorder),
        )
        with pytest.raises(CompactionDisabled, match="no policy to compact toward"):
            await compaction.compact(limit=10)
        assert recorder.asked == []

    asyncio.run(body())


def test_a_batch_reports_every_outcome_it_saw() -> None:
    async def body() -> None:
        backend = MemoryStore()
        prose = b"repeated prose that zstd eats alive. " * 100
        candidates = (
            await seed(backend, "objects/prose", prose),
            await seed(backend, "objects/dense", Random(11).randbytes(4096)),
            StoredObject(
                id=uuid7(),
                key="objects/absent",
                content_hash=sql.uuid8(b"absent"),
                size=6,
                stored_size=6,
                encoding=Blob.Encoding.identity,
            ),
        )
        recorder = Recorder(candidates)

        report = await ArtifactCompaction(
            store(backend), cast(ArtifactRepository, recorder)
        ).compact(limit=3)

        assert (report.examined, report.rewritten, report.failed) == (3, 1, 1)
        assert report.stored_bytes_before == sum(item.stored_size for item in candidates)
        assert report.reclaimed > 0

    asyncio.run(body())
