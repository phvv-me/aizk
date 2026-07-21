from collections.abc import Iterator

import dbutil
import pytest
from doubles import FakeLLM, RecordingEmbedder
from id_factory import uuid5
from pydantic import UUID5, UUID7
from sqlmodel import select

from aizk.config import settings
from aizk.graph.insight import derive_insights, kept_observations
from aizk.graph.models import InsightReport, Observation
from aizk.ontology import System
from aizk.store import Entity, Fact


@pytest.fixture
def owner(migrated_db: None) -> Iterator[UUID5 | UUID7]:
    pid = uuid5()

    async def setup() -> None:
        await dbutil.reset_db()

    dbutil.run(setup())
    yield pid


def test_kept_observations_gates_on_significance_and_caps_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = InsightReport(
        observations=[
            Observation(statement="weak", significance=0.2),
            Observation(statement="strong", significance=0.9),
            Observation(statement="mid", significance=0.7),
        ]
    )
    monkeypatch.setattr(settings, "insight_min_significance", 0.6)
    monkeypatch.setattr(settings, "insight_max", 1)
    assert [obs.statement for obs in kept_observations(report)] == ["strong"]


async def seed_two_facts(owner: UUID5 | UUID7) -> None:
    subject = uuid5()
    async with dbutil.actor(owner) as session:
        session.add(Entity.Content(id=subject, name="alice", type="author", embedding=None))
        await session.flush()
        session.add(Entity.Claim(content_id=subject, created_by=owner, scopes=[owner]))
        for index in range(2):
            content = Fact.Content(
                id=uuid5(),
                subject_id=subject,
                predicate="related_to",
                statement=f"alice fact {index}",
                embedding=None,
            )
            session.add(content)
            await session.flush()
            session.add(Fact.Claim(content_id=content.id, created_by=owner, scopes=[owner]))


async def observed(user: UUID5 | UUID7) -> list[str]:
    async with dbutil.actor(user) as session:
        return list(
            await session.exec(
                select(Fact.Live.statement).where(Fact.Live.predicate == System.Relation.OBSERVES)
            )
        )


@pytest.mark.parametrize("scenario", ["empty", "weak", "mixed"])
def test_derive_insights_gates_observations_and_is_idempotent(
    owner: UUID5 | UUID7,
    fake_llm: FakeLLM,
    fake_embedder: RecordingEmbedder,
    scenario: str,
) -> None:
    observations = {
        "empty": [],
        "weak": [Observation(statement="a shallow restatement", significance=0.1)],
        "mixed": [
            Observation(statement="alice drives the project", significance=0.95),
            Observation(statement="a shallow restatement", significance=0.1),
        ],
    }[scenario]
    fake_llm.register(
        InsightReport,
        InsightReport(observations=observations),
    )

    async def probe() -> tuple[int, int, list[str]]:
        if scenario != "empty":
            await seed_two_facts(owner)
        written = await derive_insights(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))
        again = await derive_insights(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))
        return written, again, await observed(owner)

    written, again, mine = dbutil.run(probe())
    assert written == (1 if scenario == "mixed" else 0)
    assert again == 0
    assert mine == (["alice drives the project"] if scenario == "mixed" else [])
