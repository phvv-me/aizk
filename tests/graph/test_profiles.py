from collections.abc import Iterator

import dbutil
import pytest
from doubles import FakeLLM, RecordingEmbedder
from id_factory import uuid5
from pydantic import UUID5, UUID7
from sqlmodel import select

from aizk.config import settings
from aizk.exceptions import NotVisibleError
from aizk.graph.profiles import build_profile, refresh_dirty_profiles, refresh_profiles
from aizk.store import Entity, Fact, Profile, Watermark


@pytest.fixture
def owner(migrated_db: None) -> Iterator[UUID5 | UUID7]:
    pid = uuid5()

    async def setup() -> None:
        await dbutil.reset_db()

    dbutil.run(setup())
    yield pid


async def seed_entity_with_facts(owner: UUID5 | UUID7, name: str, count: int = 2) -> UUID5 | UUID7:
    subject = uuid5()
    async with dbutil.actor(owner) as session:
        session.add(Entity.Content(id=subject, name=name, type="concept", embedding=None))
        await session.flush()
        session.add(Entity.Claim(content_id=subject, created_by=owner, scopes=[owner]))
        for index in range(count):
            content = Fact.Content(
                id=uuid5(),
                subject_id=subject,
                predicate="related_to",
                statement=f"{name} fact {index}",
                embedding=None,
            )
            session.add(content)
            await session.flush()
            session.add(Fact.Claim(content_id=content.id, created_by=owner, scopes=[owner]))
    return subject


async def stored_summary(owner: UUID5 | UUID7, subject: UUID5 | UUID7) -> str | None:
    async with dbutil.actor(owner) as session:
        return (
            await session.exec(select(Profile.summary).where(Profile.subject_id == subject))
        ).first()


def test_build_profile_upserts_one_row_and_is_idempotent(
    owner: UUID5 | UUID7, fake_llm: FakeLLM, fake_embedder: RecordingEmbedder
) -> None:
    async def probe() -> tuple[UUID5 | UUID7, UUID5 | UUID7, str | None]:
        subject = await seed_entity_with_facts(owner, "Leech lattice")
        first = await build_profile(
            subject, fake_llm.llm, fake_embedder, scopes=frozenset({owner})
        )
        second = await build_profile(
            subject, fake_llm.llm, fake_embedder, scopes=frozenset({owner})
        )
        return first, second, await stored_summary(owner, subject)

    first, second, summary = dbutil.run(probe())
    assert first == second
    assert summary is not None and summary.strip()


@pytest.mark.parametrize("entity_count", [0, 2], ids=["empty", "related"])
def test_refresh_profiles_rebuilds_the_visible_related_graph_in_one_batch(
    owner: UUID5 | UUID7, fake_llm: FakeLLM, fake_embedder: RecordingEmbedder, entity_count: int
) -> None:
    async def probe() -> tuple[int, str | None, str | None]:
        if not entity_count:
            return (
                await refresh_profiles(fake_llm.llm, fake_embedder, scopes=frozenset({owner})),
                None,
                None,
            )
        alpha = await seed_entity_with_facts(owner, "alpha")
        beta = await seed_entity_with_facts(owner, "beta")
        async with dbutil.actor(owner) as session:
            relation = Fact.Content(
                id=uuid5(),
                subject_id=alpha,
                object_id=beta,
                predicate="related_to",
                statement="alpha relates to beta",
            )
            session.add(relation)
            await session.flush()
            session.add(Fact.Claim(content_id=relation.id, created_by=owner, scopes=[owner]))
        count = await refresh_profiles(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))
        return count, await stored_summary(owner, alpha), await stored_summary(owner, beta)

    count, alpha, beta = dbutil.run(probe())
    assert count == entity_count
    assert (alpha is not None and beta is not None) is bool(entity_count)


def test_build_profile_refuses_an_invisible_entity(
    owner: UUID5 | UUID7, fake_llm: FakeLLM, fake_embedder: RecordingEmbedder
) -> None:
    async def probe() -> None:
        with pytest.raises(NotVisibleError, match="not visible"):
            await build_profile(uuid5(), fake_llm.llm, fake_embedder, scopes=frozenset({owner}))

    dbutil.run(probe())


def test_dirty_profiles_run_in_bounded_batches_and_consume_their_watermarks(
    owner: UUID5 | UUID7,
    fake_llm: FakeLLM,
    fake_embedder: RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "profile_batch_size", 1)

    async def probe() -> tuple[list[int], list[str | None], dict[str, int]]:
        alpha = await seed_entity_with_facts(owner, "alpha")
        beta = await seed_entity_with_facts(owner, "beta")
        key = frozenset({owner})
        async with dbutil.actor(owner) as session:
            await Watermark.bump_many(
                session,
                key,
                Watermark.Kind.entity_dirty,
                [str(alpha), str(beta)],
            )
        counts = [
            await refresh_dirty_profiles(fake_llm.llm, fake_embedder, scopes=key),
            await refresh_dirty_profiles(fake_llm.llm, fake_embedder, scopes=key),
            await refresh_dirty_profiles(fake_llm.llm, fake_embedder, scopes=key),
        ]
        async with dbutil.actor(owner) as session:
            pending = await Watermark.pending_refs(
                session, key, Watermark.Kind.entity_dirty, settings.profile_batch_size
            )
        return (
            counts,
            [await stored_summary(owner, alpha), await stored_summary(owner, beta)],
            pending,
        )

    counts, summaries, pending = dbutil.run(probe())

    assert counts == [1, 1, 0]
    assert all(summaries)
    assert pending == {}
