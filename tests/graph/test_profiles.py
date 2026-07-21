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


@pytest.mark.parametrize("visible", [True, False], ids=["visible", "invisible"])
def test_build_profile_upserts_visible_entities_and_rejects_invisible_ones(
    owner: UUID5 | UUID7,
    fake_llm: FakeLLM,
    fake_embedder: RecordingEmbedder,
    visible: bool,
) -> None:
    async def probe() -> tuple[UUID5 | UUID7, UUID5 | UUID7, str | None]:
        subject = await seed_entity_with_facts(owner, "Leech lattice") if visible else uuid5()
        if not visible:
            with pytest.raises(NotVisibleError, match="not visible"):
                await build_profile(
                    subject, fake_llm.llm, fake_embedder, scopes=frozenset({owner})
                )
            return subject, subject, None
        first = await build_profile(
            subject, fake_llm.llm, fake_embedder, scopes=frozenset({owner})
        )
        second = await build_profile(
            subject, fake_llm.llm, fake_embedder, scopes=frozenset({owner})
        )
        return first, second, await stored_summary(owner, subject)

    first, second, summary = dbutil.run(probe())
    assert first == second
    assert bool(summary and summary.strip()) is visible


@pytest.mark.parametrize("scenario", ["empty", "related", "dirty"])
def test_refresh_profiles_batches_visible_and_dirty_entities(
    owner: UUID5 | UUID7,
    fake_llm: FakeLLM,
    fake_embedder: RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    async def probe() -> None:
        if scenario == "empty":
            count = await refresh_profiles(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))
            assert count == 0
            return
        alpha = await seed_entity_with_facts(owner, "alpha")
        beta = await seed_entity_with_facts(owner, "beta")
        key = frozenset({owner})
        if scenario == "dirty":
            monkeypatch.setattr(settings, "profile_batch_size", 1)
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
            assert counts == [1, 1, 0]
            assert all([await stored_summary(owner, alpha), await stored_summary(owner, beta)])
            assert pending == {}
            return
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
        assert count == 2
        assert await stored_summary(owner, alpha)
        assert await stored_summary(owner, beta)

    dbutil.run(probe())
