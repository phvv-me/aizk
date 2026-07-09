import uuid
from collections.abc import Iterator

import dbutil
import pytest
from sqlalchemy import select

from aizk.exceptions import NotVisibleError
from aizk.graph.profiles import build_profile, refresh_profiles
from aizk.store import EntityClaim, EntityContent, FactClaim, FactContent, Profile, acting_as


@pytest.fixture
def owner(migrated_db: None) -> Iterator[uuid.UUID]:
    """A freshly reset schema minting one owner id, the owner every profile body acts as."""
    pid = uuid.uuid4()

    async def setup() -> None:
        await dbutil.reset_db()

    dbutil.run(setup())
    yield pid


async def seed_entity_with_facts(owner: uuid.UUID, name: str, count: int = 2) -> uuid.UUID:
    """Plant one entity and `count` latest facts naming it, the material a profile summarizes."""
    subject = uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(EntityContent(id=subject, name=name, type="concept", embedding=None))
        await session.flush()
        session.add(EntityClaim(content_id=subject, owner_id=owner))
        for index in range(count):
            content = FactContent(
                subject_id=subject,
                predicate="related_to",
                statement=f"{name} fact {index}",
                embedding=None,
            )
            session.add(content)
            await session.flush()
            session.add(FactClaim(content_id=content.id, owner_id=owner))
    return subject


async def stored_summary(owner: uuid.UUID, subject: uuid.UUID) -> str | None:
    """Read the stored profile summary of one subject under the owner's visibility."""
    async with acting_as(owner) as session:
        return await session.scalar(select(Profile.summary).where(Profile.subject_id == subject))


@pytest.mark.usefixtures("fake_embedder")
def test_build_profile_upserts_one_row_and_is_idempotent(
    owner: uuid.UUID, fake_llm: object
) -> None:
    """Two builds of one entity overwrite the same profile row, its summary stored and readable.

    The one-row-per-subject upsert is what keeps a weekly rebuild from piling up portraits, so the
    second build returns the same id and the stored paragraph reads back non-empty.
    """

    async def probe() -> tuple[uuid.UUID, uuid.UUID, str | None]:
        subject = await seed_entity_with_facts(owner, "Leech lattice")
        first = await build_profile(subject, user_id=owner)
        second = await build_profile(subject, user_id=owner)
        return first, second, await stored_summary(owner, subject)

    first, second, summary = dbutil.run(probe())
    assert first == second
    assert summary is not None and summary.strip()


@pytest.mark.usefixtures("fake_embedder")
def test_refresh_profiles_rebuilds_every_writable_entity(
    owner: uuid.UUID, fake_llm: object
) -> None:
    """The full pass rolls a profile for each writable entity, the count it reports back."""

    async def probe() -> tuple[int, str | None, str | None]:
        alpha = await seed_entity_with_facts(owner, "alpha")
        beta = await seed_entity_with_facts(owner, "beta")
        count = await refresh_profiles(user_id=owner)
        return count, await stored_summary(owner, alpha), await stored_summary(owner, beta)

    count, alpha, beta = dbutil.run(probe())
    assert count == 2
    assert alpha is not None and beta is not None


@pytest.mark.usefixtures("fake_embedder")
def test_build_profile_refuses_an_invisible_entity(owner: uuid.UUID, fake_llm: object) -> None:
    """A subject the user cannot see raises rather than minting a profile from nothing."""

    async def probe() -> None:
        with pytest.raises(NotVisibleError, match="not visible"):
            await build_profile(uuid.uuid4(), user_id=owner)

    dbutil.run(probe())
