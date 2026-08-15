import asyncio
from datetime import UTC, datetime, timedelta

import dbutil
import pytest
from factories import seed_artifact
from id_factory import uuid5
from patos import sql
from sqlmodel import select

from aizk.artifacts.repository import ArtifactRepository
from aizk.storage import IntegrityCheck, StoredBytes
from aizk.store import Blob, ObjectRetirement
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")


def test_repository_compare_and_swap_has_one_winner_and_is_idempotent() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        stored = await seed_artifact(owner, [owner], size=4096)
        repository = ArtifactRepository()
        observed = (await repository.compaction_candidates(level=9, limit=1))[0]
        replacements = tuple(
            StoredBytes(
                key=f"objects/candidate-{ordinal}",
                content_hash=observed.content_hash,
                size=observed.size,
                stored_size=observed.stored_size // (ordinal + 2),
                encoding=Blob.Encoding.zstd,
                encoding_level=9,
                etag=f"etag-{ordinal}",
                version=f"version-{ordinal}",
            )
            for ordinal in range(2)
        )
        verified_at = datetime.now(UTC)
        retire_after = verified_at + timedelta(hours=1)

        results = await asyncio.gather(
            *(
                repository.record_compaction(
                    observed,
                    9,
                    verified_at,
                    item,
                    retire_after=retire_after,
                )
                for item in replacements
            )
        )

        assert sorted(results) == [False, True]
        winner = replacements[results.index(True)]
        async with User.system().owner as session:
            blob = await session.get(Blob, stored.blob.id)
        assert blob is not None
        assert blob.storage_key == winner.key
        assert blob.storage_version == winner.version
        assert blob.encoding_level == 9
        assert blob.content_hash == observed.content_hash
        assert blob.size == observed.size
        async with User.private(owner) as session:
            assert list(await session.exec(select(ObjectRetirement))) == []
        async with User.system().owner as session:
            retirements = list(await session.exec(select(ObjectRetirement)))
        assert len(retirements) == 1
        assert retirements[0].storage_key == observed.key
        assert retirements[0].delete_after == retire_after
        assert await repository.claim_retirements(verified_at, retire_after, limit=1) == ()
        lease_until = retire_after + timedelta(minutes=5)
        claimed = await repository.claim_retirements(retire_after, lease_until, limit=1)
        assert len(claimed) == 1
        assert claimed[0].delete_after == lease_until
        assert await repository.claim_retirements(retire_after, lease_until, limit=1) == ()
        async with User.system().owner as session:
            blob = await session.get(Blob, stored.blob.id)
            assert blob is not None
            blob.storage_key = claimed[0].key
        assert not await repository.forget_retirement(claimed[0])
        async with User.system().owner as session:
            blob = await session.get(Blob, stored.blob.id)
            assert blob is not None
            blob.storage_key = winner.key
        assert await repository.retirement_is_unreferenced(claimed[0])
        assert await repository.forget_retirement(claimed[0])
        assert not await repository.record_compaction(
            observed,
            9,
            verified_at,
            winner,
            retire_after=retire_after,
        )

    dbutil.run(body())


def test_repository_rejects_stale_storage_identity_and_policy() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        await seed_artifact(owner, [owner], size=4096)
        repository = ArtifactRepository()
        observed = (await repository.compaction_candidates(level=9, limit=1))[0]
        verified_at = datetime.now(UTC)

        assert not await repository.record_compaction(
            observed.model_copy(update={"version": "stale"}), 9, verified_at
        )
        assert not await repository.record_compaction(
            observed.model_copy(update={"encoding_level": 1}), 9, verified_at
        )
        current = (await repository.compaction_candidates(level=9, limit=1))[0]
        assert current == observed

    dbutil.run(body())


def test_a_stale_integrity_failure_cannot_mark_the_replacement_failed() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        stored = await seed_artifact(owner, [owner], size=4096)
        repository = ArtifactRepository()
        observed = (await repository.integrity_candidates(datetime.now(UTC), limit=1))[0]
        verified_at = datetime.now(UTC)
        replacement = StoredBytes(
            key="objects/replacement-after-integrity-read",
            content_hash=observed.content_hash,
            size=observed.size,
            stored_size=observed.stored_size // 2,
            encoding=Blob.Encoding.zstd,
            encoding_level=9,
        )
        assert await repository.record_compaction(
            observed,
            9,
            verified_at,
            replacement,
            retire_after=verified_at + timedelta(hours=1),
        )

        stale_failure = IntegrityCheck(
            observed=observed,
            error="FileNotFoundError: stale layout was retired",
        )
        assert await repository.record_integrity((stale_failure,), datetime.now(UTC)) == ()

        async with User.system().owner as session:
            blob = await session.get(Blob, stored.blob.id)
        assert blob is not None
        assert blob.storage_key == replacement.key
        assert blob.integrity_checked_at == verified_at
        assert blob.integrity_error is None

    dbutil.run(body())


@pytest.mark.parametrize("changed", ["content", "original_size", "key", "stored_size", "level"])
def test_repository_rejects_invalid_replacement_identity(changed: str) -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        await seed_artifact(owner, [owner], size=4096)
        repository = ArtifactRepository()
        observed = (await repository.compaction_candidates(level=9, limit=1))[0]
        replacement = StoredBytes(
            key="objects/replacement",
            content_hash=observed.content_hash,
            size=observed.size,
            stored_size=observed.stored_size // 2,
            encoding=Blob.Encoding.zstd,
            encoding_level=9,
        )
        changes = {
            "content": {"content_hash": sql.uuid8(b"different")},
            "original_size": {"size": observed.size + 1},
            "key": {"key": observed.key},
            "stored_size": {"stored_size": observed.stored_size},
            "level": {"encoding_level": 8},
        }

        with pytest.raises(ValueError, match="compacted"):
            await repository.record_compaction(
                observed, 9, datetime.now(UTC), replacement.model_copy(update=changes[changed])
            )

        with pytest.raises(ValueError, match="defer"):
            await repository.record_compaction(observed, 9, datetime.now(UTC), replacement)
        with pytest.raises(ValueError, match="nothing to retire"):
            await repository.record_compaction(
                observed,
                9,
                datetime.now(UTC),
                retire_after=datetime.now(UTC) + timedelta(hours=1),
            )

    dbutil.run(body())
