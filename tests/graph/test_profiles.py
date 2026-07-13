import uuid
from collections.abc import Iterator

import dbutil
import pytest
from sqlmodel import select

from aizk.exceptions import NotVisibleError
from aizk.graph.profiles import build_profile, refresh_profiles
from aizk.store import EntityClaim, EntityContent, FactClaim, FactContent, Profile


@pytest.fixture
def owner(migrated_db: None) -> Iterator[uuid.UUID]:
    pid = uuid.uuid4()

    async def setup() -> None:
        await dbutil.reset_db()

    dbutil.run(setup())
    yield pid


async def seed_entity_with_facts(owner: uuid.UUID, name: str, count: int = 2) -> uuid.UUID:
    subject = uuid.uuid4()
    async with dbutil.actor(owner) as session:
        session.add(EntityContent(id=subject, name=name, type="concept", embedding=None))
        await session.flush()
        session.add(EntityClaim(content_id=subject, created_by=owner, scopes=[owner]))
        for index in range(count):
            content = FactContent(
                subject_id=subject,
                predicate="related_to",
                statement=f"{name} fact {index}",
                embedding=None,
            )
            session.add(content)
            await session.flush()
            session.add(FactClaim(content_id=content.id, created_by=owner, scopes=[owner]))
    return subject


async def stored_summary(owner: uuid.UUID, subject: uuid.UUID) -> str | None:
    async with dbutil.actor(owner) as session:
        return (
            await session.exec(select(Profile.summary).where(Profile.subject_id == subject))
        ).first()


@pytest.mark.usefixtures("fake_embedder")
def test_build_profile_upserts_one_row_and_is_idempotent(
    owner: uuid.UUID, fake_llm: object
) -> None:
    async def probe() -> tuple[uuid.UUID, uuid.UUID, str | None]:
        subject = await seed_entity_with_facts(owner, "Leech lattice")
        first = await build_profile(subject, scopes=frozenset({owner}))
        second = await build_profile(subject, scopes=frozenset({owner}))
        return first, second, await stored_summary(owner, subject)

    first, second, summary = dbutil.run(probe())
    assert first == second
    assert summary is not None and summary.strip()


@pytest.mark.usefixtures("fake_embedder")
@pytest.mark.parametrize("entity_count", [0, 2], ids=["empty", "related"])
def test_refresh_profiles_rebuilds_the_visible_related_graph_in_one_batch(
    owner: uuid.UUID, fake_llm: object, entity_count: int
) -> None:
    async def probe() -> tuple[int, str | None, str | None]:
        if not entity_count:
            return await refresh_profiles(scopes=frozenset({owner})), None, None
        alpha = await seed_entity_with_facts(owner, "alpha")
        beta = await seed_entity_with_facts(owner, "beta")
        async with dbutil.actor(owner) as session:
            relation = FactContent(
                subject_id=alpha,
                object_id=beta,
                predicate="related_to",
                statement="alpha relates to beta",
            )
            session.add(relation)
            await session.flush()
            session.add(FactClaim(content_id=relation.id, created_by=owner, scopes=[owner]))
        count = await refresh_profiles(scopes=frozenset({owner}))
        return count, await stored_summary(owner, alpha), await stored_summary(owner, beta)

    count, alpha, beta = dbutil.run(probe())
    assert count == entity_count
    assert (alpha is not None and beta is not None) is bool(entity_count)


@pytest.mark.usefixtures("fake_embedder")
def test_build_profile_refuses_an_invisible_entity(owner: uuid.UUID, fake_llm: object) -> None:
    async def probe() -> None:
        with pytest.raises(NotVisibleError, match="not visible"):
            await build_profile(uuid.uuid4(), scopes=frozenset({owner}))

    dbutil.run(probe())
