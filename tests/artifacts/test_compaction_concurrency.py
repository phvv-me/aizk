import asyncio
from datetime import UTC, datetime, timedelta
from random import Random
from typing import cast

import pytest
from id_factory import uuid7
from obstore.store import MemoryStore
from patos import sql
from pydantic import UUID8

from aizk.artifacts.repository import ArtifactRepository
from aizk.artifacts.service import ArtifactCompaction, ArtifactRetirement
from aizk.storage import ByteStore, RetiredObject, StoredBytes, StoredObject
from aizk.store import Blob


class TrackingStorage:
    """Expose object lifecycle events while retaining the production byte semantics."""

    def __init__(self, backend: MemoryStore) -> None:
        self.backend = backend
        self.store = ByteStore(
            backend=backend,
            upload_byte_limit=1 << 20,
            internal_download_lifetime=timedelta(minutes=5),
        )
        self.puts: list[str] = []
        self.deletes: list[str] = []

    @property
    def compression_enabled(self) -> bool:
        return self.store.compression_enabled

    @property
    def compression_level(self) -> int:
        return self.store.compression_level

    @property
    def retirement_grace(self) -> timedelta:
        return self.store.retirement_grace

    async def get(
        self,
        key: str,
        *,
        encoding: Blob.Encoding = Blob.Encoding.identity,
        expected_size: int | None = None,
        expected_hash: UUID8 | None = None,
        version: str | None = None,
    ) -> bytes:
        return await self.store.get(
            key,
            encoding=encoding,
            expected_size=expected_size,
            expected_hash=expected_hash,
            version=version,
        )

    async def put(self, data: bytes) -> StoredBytes:
        stored = await self.store.put(data)
        self.puts.append(stored.key)
        return stored

    async def delete(self, key: str) -> None:
        self.deletes.append(key)
        await self.store.delete(key)


class InterleavedRepository:
    """Hold both workers after their writes, then emulate one atomic compare and swap."""

    def __init__(self, current: StoredObject) -> None:
        self.current = current
        self.arrivals = 0
        self.ready = asyncio.Event()
        self.lock = asyncio.Lock()
        self.winner: StoredBytes | None = None

    async def record_compaction(
        self,
        observed: StoredObject,
        level: int,
        verified_at: datetime,
        replacement: StoredBytes | None = None,
        retire_after: datetime | None = None,
    ) -> bool:
        del verified_at, retire_after
        assert replacement is not None
        self.arrivals += 1
        if self.arrivals == 2:
            self.ready.set()
        await self.ready.wait()
        async with self.lock:
            if observed != self.current:
                return False
            self.winner = replacement
            self.current = StoredObject(
                id=observed.id,
                key=replacement.key,
                content_hash=observed.content_hash,
                size=observed.size,
                stored_size=replacement.stored_size,
                encoding=replacement.encoding,
                encoding_level=level,
                version=replacement.version,
            )
            return True


def test_interleaved_workers_keep_only_the_winning_exact_representation() -> None:
    async def body() -> None:
        backend = MemoryStore()
        payload = b"one immutable artifact. " * 400
        old_key = "objects/legacy"
        await backend.put_async(old_key, payload, mode="create")
        observed = StoredObject(
            id=uuid7(),
            key=old_key,
            content_hash=sql.uuid8(payload),
            size=len(payload),
            stored_size=len(payload),
            encoding=Blob.Encoding.identity,
        )
        storage = TrackingStorage(backend)
        repository = InterleavedRepository(observed)
        compaction = ArtifactCompaction(
            cast(ByteStore, storage), cast(ArtifactRepository, repository)
        )

        outcomes = await asyncio.gather(
            compaction.rewrite(observed, 9),
            compaction.rewrite(observed, 9),
        )

        assert sum(outcome.rewritten for outcome in outcomes) == 1
        assert all(outcome.error is None for outcome in outcomes)
        assert repository.winner is not None
        winner_key = repository.winner.key
        losing_key = next(key for key in storage.puts if key != winner_key)
        assert storage.deletes.count(old_key) == 0
        assert storage.deletes.count(losing_key) == 1
        assert winner_key not in storage.deletes
        keys = [entry["path"] async for batch in backend.list() for entry in batch]
        assert set(keys) == {old_key, winner_key}
        # A reader that loaded the old pointer before the CAS remains valid afterward.
        assert bytes(await (await backend.get_async(old_key)).bytes_async()) == payload
        assert (
            await storage.store.get(
                winner_key,
                encoding=repository.winner.encoding,
                expected_size=observed.size,
                expected_hash=observed.content_hash,
                version=repository.winner.version,
            )
            == payload
        )

    asyncio.run(body())


def test_a_compare_and_swap_loss_cleans_only_the_new_candidate() -> None:
    class LosingRepository:
        async def record_compaction(
            self,
            observed: StoredObject,
            level: int,
            verified_at: datetime,
            replacement: StoredBytes | None = None,
            retire_after: datetime | None = None,
        ) -> bool:
            del observed, level, verified_at, replacement, retire_after
            return False

    async def body() -> None:
        backend = MemoryStore()
        payload = b"same immutable artifact. " * 400
        old_key = "objects/current"
        await backend.put_async(old_key, payload, mode="create")
        observed = StoredObject(
            id=uuid7(),
            key=old_key,
            content_hash=sql.uuid8(payload),
            size=len(payload),
            stored_size=len(payload),
            encoding=Blob.Encoding.identity,
        )
        storage = TrackingStorage(backend)
        outcome = await ArtifactCompaction(
            cast(ByteStore, storage), cast(ArtifactRepository, LosingRepository())
        ).rewrite(observed, 9)

        assert not outcome.rewritten
        assert outcome.conflicted
        assert outcome.error is None
        assert storage.deletes == storage.puts
        keys = [entry["path"] async for batch in backend.list() for entry in batch]
        assert keys == [old_key]
        assert bytes(await (await backend.get_async(old_key)).bytes_async()) == payload

    asyncio.run(body())


def test_a_nonreplacement_compare_and_swap_loss_is_reported_as_a_conflict() -> None:
    class LosingRepository:
        async def record_compaction(
            self,
            observed: StoredObject,
            level: int,
            verified_at: datetime,
            replacement: StoredBytes | None = None,
            retire_after: datetime | None = None,
        ) -> bool:
            del observed, level, verified_at, replacement, retire_after
            return False

    async def body() -> None:
        backend = MemoryStore()
        payload = Random(19).randbytes(4096)
        old_key = "objects/current-dense"
        await backend.put_async(old_key, payload, mode="create")
        observed = StoredObject(
            id=uuid7(),
            key=old_key,
            content_hash=sql.uuid8(payload),
            size=len(payload),
            stored_size=len(payload),
            encoding=Blob.Encoding.identity,
        )
        storage = TrackingStorage(backend)

        outcome = await ArtifactCompaction(
            cast(ByteStore, storage), cast(ArtifactRepository, LosingRepository())
        ).rewrite(observed, 9)

        assert not outcome.rewritten
        assert outcome.conflicted
        assert outcome.error is None
        assert storage.deletes == storage.puts
        assert bytes(await (await backend.get_async(old_key)).bytes_async()) == payload

    asyncio.run(body())


def test_retirement_deletes_only_a_due_unreferenced_layout() -> None:
    class RetirementRepository:
        def __init__(self, retired: RetiredObject) -> None:
            self.retired = retired
            self.forgotten: list[RetiredObject] = []

        async def claim_retirements(
            self,
            delete_before: datetime,
            lease_until: datetime,
            limit: int,
        ) -> tuple[RetiredObject, ...]:
            assert delete_before.tzinfo is UTC
            assert lease_until > delete_before
            assert limit == 1
            return (self.retired.model_copy(update={"delete_after": lease_until}),)

        async def retirement_is_unreferenced(self, retired: RetiredObject) -> bool:
            assert retired.key == self.retired.key
            return True

        async def forget_retirement(self, retired: RetiredObject) -> bool:
            self.forgotten.append(retired)
            return True

    async def body() -> None:
        backend = MemoryStore()
        key = "objects/retired"
        payload = b"old layout"
        await backend.put_async(key, payload, mode="create")
        retired = RetiredObject(
            id=uuid7(),
            key=key,
            stored_size=len(payload),
            delete_after=datetime.now(UTC),
        )
        repository = RetirementRepository(retired)
        storage = TrackingStorage(backend)

        report = await ArtifactRetirement(
            cast(ByteStore, storage),
            cast(ArtifactRepository, repository),
        ).collect(limit=1)

        assert report.model_dump() == {
            "examined": 1,
            "deleted": 1,
            "failed": 0,
            "reclaimed": len(payload),
        }
        assert storage.deletes == [key]
        assert len(repository.forgotten) == 1
        with pytest.raises(FileNotFoundError):
            await backend.get_async(key)

    asyncio.run(body())


def test_retirement_retains_referenced_failed_and_unconfirmed_layouts() -> None:
    class RetirementRepository:
        def __init__(self, retired: tuple[RetiredObject, ...]) -> None:
            self.retired = retired

        async def claim_retirements(
            self,
            delete_before: datetime,
            lease_until: datetime,
            limit: int,
        ) -> tuple[RetiredObject, ...]:
            assert delete_before.tzinfo is UTC
            assert lease_until > delete_before
            assert limit == len(self.retired)
            return self.retired

        async def retirement_is_unreferenced(self, retired: RetiredObject) -> bool:
            return retired.key != "objects/referenced"

        async def forget_retirement(self, retired: RetiredObject) -> bool:
            return retired.key != "objects/unconfirmed"

    class RetirementStorage:
        async def delete(self, key: str) -> None:
            if key == "objects/failed":
                raise OSError("offline")

    async def body() -> None:
        retired = tuple(
            RetiredObject(
                id=uuid7(),
                key=f"objects/{name}",
                stored_size=10,
                delete_after=datetime.now(UTC),
            )
            for name in ("referenced", "failed", "unconfirmed")
        )
        report = await ArtifactRetirement(
            cast(ByteStore, RetirementStorage()),
            cast(ArtifactRepository, RetirementRepository(retired)),
        ).collect(limit=len(retired))

        assert report.model_dump() == {
            "examined": 3,
            "deleted": 0,
            "failed": 2,
            "reclaimed": 0,
        }

    asyncio.run(body())
