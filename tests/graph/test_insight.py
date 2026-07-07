import uuid
from collections.abc import Iterator

import dbutil
import pytest
from sqlalchemy import select

from aizk.config import settings
from aizk.extract import ontology
from aizk.graph.insight import derive_insights, kept_observations
from aizk.graph.models import InsightReport, Observation
from aizk.store import EntityClaim, EntityContent, FactClaim, FactContent, LiveFact, acting_as


@pytest.fixture
def owner(migrated_db: None) -> Iterator[uuid.UUID]:
    """A freshly reset schema seeding one principal, the graph the reflective pass reads."""
    pid = uuid.uuid4()

    async def setup() -> None:
        await dbutil.reset_db()
        await dbutil.seed_principal(pid)

    dbutil.run(setup())
    yield pid


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
    assert [obs.statement for obs in kept_observations(report)] == ["strong"]


async def seed_two_facts(owner: uuid.UUID) -> None:
    """Plant one entity and two latest facts, the grounding the reflective pass reasons over."""
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


async def observed(principal: uuid.UUID) -> list[str]:
    """The observes-fact statements one principal reads in its own graph."""
    async with acting_as(principal) as session:
        return list(
            await session.scalars(
                select(LiveFact.statement).where(LiveFact.predicate == ontology.OBSERVES)
            )
        )


@pytest.mark.usefixtures("fake_embedder")
def test_insight_writes_only_the_gated_observation_and_is_idempotent(
    owner: uuid.UUID, fake_llm: object
) -> None:
    """The pass writes the significant observation under the owner and a rerun changes nothing.

    The gate keeps the low-significance candidate out of the graph, the surviving observation lands
    as an observes fact hanging off the owner's observation node, and the content-addressed id
    makes a second pass find the same claim and write nothing.
    """
    fake_llm.register(
        InsightReport,
        InsightReport(
            observations=[
                Observation(statement="alice drives the project", significance=0.95),
                Observation(statement="a shallow restatement", significance=0.1),
            ]
        ),
    )

    async def probe() -> tuple[int, int, list[str]]:
        await seed_two_facts(owner)
        written = await derive_insights(principal_id=owner)
        again = await derive_insights(principal_id=owner)
        return written, again, await observed(owner)

    written, again, mine = dbutil.run(probe())
    assert written == 1  # only the significant observation cleared the gate
    assert again == 0  # the content-addressed id makes a rerun idempotent
    assert mine == ["alice drives the project"]


@pytest.mark.usefixtures("fake_embedder")
def test_insight_skips_a_graph_with_too_few_facts(owner: uuid.UUID, fake_llm: object) -> None:
    """A graph without at least two facts is left untouched, so the pass never reasons blind."""
    assert dbutil.run(derive_insights(principal_id=owner)) == 0


@pytest.mark.usefixtures("fake_embedder")
def test_insight_writes_nothing_when_no_observation_clears_the_gate(
    owner: uuid.UUID, fake_llm: object
) -> None:
    """When every candidate falls below the significance floor the graph gains no observation."""
    fake_llm.register(
        InsightReport,
        InsightReport(
            observations=[Observation(statement="a shallow restatement", significance=0.1)]
        ),
    )

    async def probe() -> int:
        await seed_two_facts(owner)
        return await derive_insights(principal_id=owner)

    assert dbutil.run(probe()) == 0
