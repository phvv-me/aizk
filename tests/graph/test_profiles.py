import asyncio
import uuid

import pytest
from graphdb import FakeLLM, owned_principal
from sqlalchemy import select

from aizk.graph.profiles import build_profile, refresh_profiles
from aizk.store import EntityClaim, EntityContent, FactClaim, FactContent, Profile, acting_as


async def seed_entity_with_facts(owner: uuid.UUID, name: str, count: int = 2) -> uuid.UUID:
    """Plant one entity and `count` latest facts naming it, the material a profile summarizes.

    owner: principal that owns the rows.
    name: surface form of the entity.
    count: how many latest facts to attach.
    """
    subject = uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(EntityContent(id=subject, name=name, type="Concept", embedding=None))
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
    """Read the stored profile summary of one subject under the owner's visibility.

    owner: principal whose visibility scopes the read.
    subject: entity whose profile summary to fetch.
    """
    async with acting_as(owner) as session:
        return await session.scalar(select(Profile.summary).where(Profile.subject_id == subject))


@pytest.mark.usefixtures("fake_embedder")
def test_build_profile_upserts_one_row_and_is_idempotent(
    fresh_principal: uuid.UUID, fake_llm: FakeLLM
) -> None:
    """Two builds of one entity overwrite the same profile row, its summary stored and readable.

    The one-row-per-subject upsert is what keeps a weekly rebuild from piling up portraits, so the
    second build returns the same id and the stored paragraph reads back non-empty.
    """
    owner = fresh_principal

    async def probe() -> tuple[uuid.UUID, uuid.UUID, str | None]:
        subject = await seed_entity_with_facts(owner, "Leech lattice")
        first = await build_profile(subject, principal_id=owner)
        second = await build_profile(subject, principal_id=owner)
        return first, second, await stored_summary(owner, subject)

    first, second, summary = asyncio.run(probe())
    assert first == second
    assert summary is not None and summary.strip()


@pytest.mark.usefixtures("fake_embedder")
def test_refresh_profiles_rebuilds_every_visible_entity(
    fresh_principal: uuid.UUID, fake_llm: FakeLLM
) -> None:
    """The full pass rolls a profile for each visible entity, the count it reports back."""
    owner = fresh_principal

    async def probe() -> tuple[int, str | None, str | None]:
        alpha = await seed_entity_with_facts(owner, "alpha")
        beta = await seed_entity_with_facts(owner, "beta")
        count = await refresh_profiles(principal_id=owner)
        return count, await stored_summary(owner, alpha), await stored_summary(owner, beta)

    count, alpha, beta = asyncio.run(probe())
    assert count == 2
    assert alpha is not None and beta is not None


@pytest.mark.usefixtures("fake_embedder", "fake_settings", "migrated_db")
def test_build_profile_refuses_an_invisible_entity() -> None:
    """A subject the principal cannot see raises rather than minting a profile from nothing."""

    async def probe() -> None:
        async with owned_principal() as owner:
            with pytest.raises(ValueError, match="not visible"):
                await build_profile(uuid.uuid4(), principal_id=owner)

    asyncio.run(probe())
