import asyncio
import uuid

import pytest
from graphdb import FakeLLM, owned_principal
from sqlalchemy import select

from aizk.config import settings
from aizk.extract.ontology import RelationType
from aizk.graph.insight import derive_insights, kept_observations
from aizk.graph.models import InsightReport, Observation
from aizk.store import EntityClaim, EntityContent, FactClaim, FactContent, LiveFact, acting_as


async def seed_two_facts(owner: uuid.UUID) -> None:
    """Plant one entity and two latest facts, the grounding the reflective pass reasons over.

    owner: principal that owns the seeded rows.
    """
    subject = uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(EntityContent(id=subject, name="alice", type="Author", embedding=None))
        await session.flush()
        session.add(EntityClaim(content_id=subject, owner_id=owner))
        for index in range(2):
            content = FactContent(
                subject_id=subject,
                predicate="related_to",
                statement=f"alice fact {index}",
                embedding=None,
            )
            session.add(content)
            await session.flush()
            session.add(FactClaim(content_id=content.id, owner_id=owner))


def test_kept_observations_gates_on_significance_and_caps_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the observations over the floor survive, highest first, capped at the write limit."""
    report = InsightReport(
        observations=[
            Observation(statement="weak", significance=0.2),
            Observation(statement="strong", significance=0.9),
            Observation(statement="mid", significance=0.7),
        ]
    )
    monkeypatch.setattr(settings, "insight_min_significance", 0.6)
    monkeypatch.setattr(settings, "insight_max", 1)
    kept = kept_observations(report)
    assert [obs.statement for obs in kept] == [
        "strong"
    ]  # over the floor, best first, capped at one


@pytest.mark.usefixtures("fake_embedder")
def test_insight_writes_only_the_gated_observation_and_never_leaks_across_principals(
    fresh_principal: uuid.UUID,
    fake_llm: FakeLLM,
) -> None:
    """The reflective pass writes the significant observation under the owner, no stranger sees it.

    The gate keeps the low-significance candidate out of the graph, the surviving observation lands
    as an observes fact hanging off the owner's observation node, a rerun is idempotent under the
    content-addressed id, and a second principal reading its own graph never sees the write, so the
    reflective lane respects the no-leak moat exactly like every other write.
    """
    owner = fresh_principal
    fake_llm.completions.responses[InsightReport] = InsightReport(
        observations=[
            Observation(statement="alice drives the project", significance=0.95),
            Observation(statement="a shallow restatement", significance=0.1),
        ]
    )

    async def observed(principal: uuid.UUID) -> list[str]:
        async with acting_as(principal) as session:
            return list(
                await session.scalars(
                    select(LiveFact.statement).where(LiveFact.predicate == RelationType.OBSERVES)
                )
            )

    async def probe() -> tuple[int, int, list[str], list[str]]:
        await seed_two_facts(owner)
        written = await derive_insights(principal_id=owner)
        again = await derive_insights(principal_id=owner)
        mine = await observed(owner)
        async with owned_principal() as stranger:
            theirs = await observed(stranger)
        return written, again, mine, theirs

    written, again, mine, theirs = asyncio.run(probe())
    assert written == 1  # only the significant observation cleared the gate
    assert again == 0  # the content-addressed id makes a rerun idempotent
    assert mine == ["alice drives the project"]  # the gated insight is stored under the owner
    assert theirs == []  # and no other principal ever sees it


@pytest.mark.usefixtures("fake_embedder")
def test_insight_skips_a_graph_with_too_few_facts_to_ground_on(
    fresh_principal: uuid.UUID,
    fake_llm: FakeLLM,
) -> None:
    """A graph without at least two facts is left untouched, so the pass never reasons blind."""
    owner = fresh_principal
    assert asyncio.run(derive_insights(principal_id=owner)) == 0


@pytest.mark.usefixtures("fake_embedder")
def test_insight_writes_nothing_when_no_observation_clears_the_gate(
    fresh_principal: uuid.UUID,
    fake_llm: FakeLLM,
) -> None:
    """When every candidate falls below the significance floor the graph gains no observation."""
    owner = fresh_principal
    fake_llm.completions.responses[InsightReport] = InsightReport(
        observations=[Observation(statement="a shallow restatement", significance=0.1)]
    )

    async def probe() -> int:
        await seed_two_facts(owner)
        return await derive_insights(principal_id=owner)

    assert asyncio.run(probe()) == 0
