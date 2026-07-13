import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from importlib import import_module

import dbutil
import pytest
from doubles import RecordingEmbedder, deterministic_vector
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Row
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.sql.selectable import Select
from sqlmodel import select

from aizk.config import settings
from aizk.extract import ontology
from aizk.retrieval import (
    Candidate,
    Lane,
    Plan,
    QueryContext,
    recall,
)
from aizk.retrieval.lanes import FactLane, VectorLane
from aizk.retrieval.recall import build_recall_statement
from aizk.store import EntityClaim, EntityContent, FactClaim, FactContent
from aizk.store.engine import bypass_rls
from aizk.store.identity import User

recall_module = import_module("aizk.retrieval.recall.orchestrator")
rescore_module = import_module("aizk.retrieval.rerank.rescore")


@pytest.fixture
def owner(migrated_db: None) -> Iterator[uuid.UUID]:
    owner_id = uuid.uuid7()
    dbutil.run(dbutil.reset_db())
    yield owner_id
    dbutil.run(dbutil.reset_db())


@pytest.fixture
def stub_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the entity gate call recall makes, since the client assumes a live sidecar."""

    async def no_entities(text: str) -> list[str]:
        del text
        return []

    monkeypatch.setattr(recall_module, "named_entities", no_entities)


def basis(primary: float = 1.0, secondary: float = 0.0) -> list[float]:
    vector = [0.0] * settings.embed_dim
    vector[0], vector[1] = primary, secondary
    return vector


async def seed_fact(
    user: User,
    statement: str,
    vector: list[float],
) -> uuid.UUID:
    entity = EntityContent(name=f"entity {uuid.uuid7()}", type=ontology.CONCEPT)
    content = FactContent(
        subject_id=entity.id,
        predicate=ontology.RELATED_TO,
        statement=statement,
        embedding=vector,
    )
    claim = FactClaim(
        content_id=content.id,
        created_by=user.id,
        scopes=sorted(user.scopes.write),
    )
    async with bypass_rls() as opened:
        opened.add(entity)
        await opened.flush()
        opened.add(
            EntityClaim(
                content_id=entity.id,
                created_by=user.id,
                scopes=sorted(user.scopes.write),
            )
        )
        opened.add(content)
        await opened.flush()
        opened.add(claim)
    return claim.id


def statement_for(dimensions: int, plan: Plan) -> Select:
    context = QueryContext(dimensions=dimensions, fuzzy=settings.graph_mention_fuzzy)
    return build_recall_statement(context, plan)


async def retrieve(
    user: User,
    vector: list[float],
    plan: Plan | None = None,
    entities: list[str] | None = None,
    lane_k: int = 8,
) -> Sequence[Row]:
    statement = statement_for(len(vector), plan if plan is not None else Plan.focused())
    params = {
        "qvec": vector,
        "qtext": "query",
        "qentities": entities or [],
        "k": lane_k,
        **settings.for_statement(statement),
    }
    async with user as session:
        result = await session.exec(statement, params=params)
        return result.all()


@given(lines=st.lists(st.text(min_size=1, max_size=40), max_size=8))
def test_candidates_round_trip_as_deeply_immutable_values(lines: list[str]) -> None:
    candidates = tuple(Candidate(lane=Lane.Kind.FACTS, line=line) for line in lines)

    assert all(
        Candidate.model_validate_json(candidate.model_dump_json()) == candidate
        for candidate in candidates
    )


@pytest.mark.parametrize(
    ("preset", "communities", "raptor", "hops", "leading_lane"),
    [
        (Plan.focused, False, False, 0, Lane.Kind.FACTS),
        (Plan.overview, True, True, 0, Lane.Kind.OVERVIEW),
        (Plan.multihop, False, False, settings.multihop_max_hops, Lane.Kind.FACTS),
        (Plan.maximal, True, True, settings.multihop_max_hops, Lane.Kind.FACTS),
    ],
)
def test_plan_presets_declare_the_composed_query(
    preset: Callable[[], Plan],
    communities: bool,
    raptor: bool,
    hops: int,
    leading_lane: Lane.Kind,
) -> None:
    plan = preset()

    assert plan.communities is communities
    assert plan.raptor is raptor
    assert plan.hops == hops
    assert plan.order[0] is leading_lane
    assert sorted(plan.order) == sorted(Lane.Kind)
    lanes = {lane.kind: lane for lane in plan.lanes}
    assert lanes[leading_lane].priority == 0
    assert (Lane.Kind.COMMUNITIES in lanes) is communities
    assert (Lane.Kind.OVERVIEW in lanes) is raptor
    facts = lanes[Lane.Kind.FACTS]
    assert isinstance(facts, FactLane)
    assert facts.hops == hops


def test_vector_lane_rejects_a_section_it_does_not_serve() -> None:
    lane = VectorLane(kind=Lane.Kind.FACTS, priority=0)
    context = QueryContext(dimensions=settings.embed_dim, fuzzy=False)

    with pytest.raises(ValueError, match="not a vector-only section"):
        lane(context)


@pytest.mark.parametrize("preset", [Plan.focused, Plan.overview, Plan.multihop, Plan.maximal])
def test_context_query_compiles_every_preset_to_one_candidate_statement(
    preset: Callable[[], Plan],
) -> None:
    plan = preset()
    sql = str(statement_for(settings.embed_dim, plan).compile(dialect=postgresql.dialect()))

    assert "ordered_context" in sql
    # Token pricing, the budget walk, and access accounting all moved into Python.
    assert "line_tokens" not in sql
    assert "header_tokens" not in sql
    assert "packed_context" not in sql
    assert "UPDATE fact_claim" not in sql
    # Every user-facing lane is always present; only graph overviews follow the plan.
    assert "session_item" in sql
    assert "FROM profile" in sql
    assert ("FROM community" in sql) is plan.communities
    assert ("max(" in sql) is plan.raptor
    if plan.hops:
        assert "mention_entity" in sql
        assert "seed_mass" in sql
        assert "entity_mass" in sql
        assert f"hop_{settings.multihop_max_hops}" in sql


@pytest.mark.parametrize("preset", [Plan.focused, Plan.overview, Plan.multihop, Plan.maximal])
def test_context_query_executes_every_preset_on_an_empty_graph(
    owner: uuid.UUID, preset: Callable[[], Plan]
) -> None:
    user = User.private(owner)
    assert dbutil.run(retrieve(user, basis(), preset())) == []


def test_recall_packs_within_budget_and_records_only_the_kept_fact(
    owner: uuid.UUID,
    fake_embedder: RecordingEmbedder,
    fake_reranker: list[list[str]],
    stub_entities: None,
) -> None:
    user = User.private(owner)
    vector = deterministic_vector("query:what holds", settings.embed_dim)

    async def probe() -> tuple[tuple[Candidate, ...], dict[uuid.UUID, int]]:
        # The small fact arrives first, so the prefix cut keeps it and stops at the
        # large one that no longer fits.
        small = await seed_fact(user, "short", vector)
        large = await seed_fact(user, "x" * 1000, vector)
        context = await recall("what holds", user=user, token_budget=20)
        async with bypass_rls() as opened:
            counts = dict(
                (
                    await opened.exec(
                        select(FactClaim.id, FactClaim.access_count).where(
                            FactClaim.id.in_([large, small])
                        )
                    )
                ).all()
            )
        return context, counts

    context, counts = dbutil.run(probe())

    assert [candidate.line for candidate in context] == ["- (related_to) short"]
    assert sorted(counts.values()) == [0, 1]


def test_context_query_relies_on_rls_for_the_complete_visibility_boundary(
    owner: uuid.UUID,
) -> None:
    visible = User.private(owner)
    hidden = User.private(uuid.uuid7())

    async def probe() -> list[str]:
        await seed_fact(visible, "visible fact", basis())
        await seed_fact(hidden, "hidden fact", basis())
        return [row._mapping["line"] for row in await retrieve(visible, basis())]

    lines = dbutil.run(probe())

    assert any("visible fact" in line for line in lines)
    assert all("hidden fact" not in line for line in lines)


def test_multihop_walk_reaches_beyond_the_local_neighbor_lane(owner: uuid.UUID) -> None:
    user = User.private(owner)

    async def probe(plan: Plan) -> list[str]:
        entities = [
            EntityContent(name=f"chain {index} {uuid.uuid7()}", type=ontology.CONCEPT)
            for index in range(4)
        ]
        async with bypass_rls() as opened:
            opened.add_all(entities)
            await opened.flush()
            opened.add_all(
                EntityClaim(
                    content_id=entity.id,
                    created_by=user.id,
                    scopes=sorted(user.scopes.write),
                )
                for entity in entities
            )
            contents = [
                FactContent(
                    subject_id=subject.id,
                    object_id=target.id,
                    predicate=ontology.RELATED_TO,
                    statement=statement,
                    embedding=vector,
                )
                for subject, target, statement, vector in (
                    (entities[0], entities[1], "first edge", basis()),
                    (entities[1], entities[2], "second edge", basis(0.0, 1.0)),
                    (entities[2], entities[3], "third edge", basis(-1.0, 0.0)),
                )
            ]
            opened.add_all(contents)
            await opened.flush()
            opened.add_all(
                FactClaim(
                    content_id=content.id,
                    created_by=user.id,
                    scopes=sorted(user.scopes.write),
                )
                for content in contents
            )
        # k also bounds the merged fact cut (k * fact_candidate_factor), so it stays at
        # two to keep the dense, neighbor, and multihop parts all inside the cut.
        rows = await retrieve(user, basis(), plan, lane_k=2)
        return [row._mapping["line"] for row in rows]

    local = dbutil.run(probe(Plan.focused()))
    dbutil.run(dbutil.reset_db())
    multihop = dbutil.run(probe(Plan.multihop()))

    assert any("second edge" in line for line in local)
    assert all("third edge" not in line for line in local)
    assert any("third edge" in line for line in multihop)


def test_fresh_and_frequent_claims_outrank_stale_twins_at_equal_distance(
    owner: uuid.UUID,
) -> None:
    user = User.private(owner)
    now = datetime.now(UTC)

    async def probe() -> list[str]:
        entity = EntityContent(name=f"subject {uuid.uuid7()}", type=ontology.CONCEPT)
        async with bypass_rls() as opened:
            opened.add(entity)
            await opened.flush()
            opened.add(EntityClaim(content_id=entity.id, created_by=user.id, scopes=[user.id]))
            for label, recorded, accessed, count in (
                ("stale fact", now - timedelta(days=200), None, 0),
                ("fresh fact", now - timedelta(days=1), now, 12),
            ):
                content = FactContent(
                    subject_id=entity.id,
                    predicate=ontology.RELATED_TO,
                    statement=label,
                    embedding=basis(),
                )
                opened.add(content)
                await opened.flush()
                claim = FactClaim(
                    content_id=content.id,
                    created_by=user.id,
                    scopes=[user.id],
                    last_accessed=accessed,
                    access_count=count,
                )
                claim.recorded = Range(recorded, None, bounds="[)")
                opened.add(claim)
        rows = await retrieve(user, basis())
        return [row._mapping["line"] for row in rows if "fact" in row._mapping["line"]]

    lines = dbutil.run(probe())

    assert lines.index("- (related_to) fresh fact") < lines.index("- (related_to) stale fact")


def test_query_entities_seed_the_graph_expansion_beyond_the_hop_budget(
    owner: uuid.UUID,
) -> None:
    user = User.private(owner)
    far = basis(-1.0, 0.0)

    async def seed_chain() -> None:
        entities = [
            EntityContent(name=f"hop {index}", type=ontology.CONCEPT) for index in range(6)
        ]
        async with bypass_rls() as opened:
            opened.add_all(entities)
            await opened.flush()
            opened.add_all(
                EntityClaim(content_id=entity.id, created_by=user.id, scopes=[user.id])
                for entity in entities
            )
            contents = [
                FactContent(
                    subject_id=entities[index].id,
                    object_id=entities[index + 1].id,
                    predicate=ontology.RELATED_TO,
                    statement=f"edge {index}",
                    embedding=basis() if index == 0 else far,
                )
                for index in range(5)
            ]
            opened.add_all(contents)
            await opened.flush()
            opened.add_all(
                FactClaim(content_id=content.id, created_by=user.id, scopes=[user.id])
                for content in contents
            )

    async def probe() -> tuple[list[str], list[str], list[str]]:
        await seed_chain()
        named = [
            row._mapping["line"]
            for row in await retrieve(user, basis(), Plan.multihop(), entities=["hop 0", "hop 5"])
        ]
        misspelled = [
            row._mapping["line"]
            for row in await retrieve(
                user, basis(), Plan.multihop(), entities=["hop 0 x", "the hop 5"]
            )
        ]
        unnamed = [row._mapping["line"] for row in await retrieve(user, basis(), Plan.multihop())]
        return named, misspelled, unnamed

    named, misspelled, unnamed = dbutil.run(probe())

    assert any("edge 4" in line for line in named)
    assert any("edge 4" in line for line in misspelled)
    assert all("edge 4" not in line for line in unnamed)


@pytest.mark.parametrize("fuzzy", [True, False], ids=["fuzzy", "exact-only"])
def test_mention_matching_compiles_trigram_fuzz_only_when_enabled(fuzzy: bool) -> None:
    context = QueryContext(dimensions=settings.embed_dim, fuzzy=fuzzy)
    sql = str(
        build_recall_statement(context, Plan.multihop()).compile(dialect=postgresql.dialect())
    )

    assert ("similarity(" in sql) is fuzzy
    assert "mention_entity" in sql


def test_recall_reranks_evidence_between_the_candidate_and_packing_phases(
    owner: uuid.UUID,
    fake_embedder: RecordingEmbedder,
    stub_entities: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User.private(owner)
    vector = deterministic_vector("query:what holds", settings.embed_dim)

    reranked: list[list[str]] = []

    async def rerank(query: str, texts: list[str]) -> list[float]:
        del query
        reranked.append(texts)
        return [1.0 if "second seeded" in text else 0.0 for text in texts]

    monkeypatch.setattr(rescore_module, "rerank", rerank)

    async def probe() -> tuple[tuple[Candidate, ...], dict[uuid.UUID, int]]:
        first = await seed_fact(user, "first seeded", vector)
        second = await seed_fact(user, "second seeded", vector)
        context = await recall("what holds", user=user, token_budget=400)
        async with bypass_rls() as opened:
            counts = dict(
                (
                    await opened.exec(
                        select(FactClaim.id, FactClaim.access_count).where(
                            FactClaim.id.in_([first, second])
                        )
                    )
                ).all()
            )
        return context, counts

    context, counts = dbutil.run(probe())

    fact_lines = [candidate.line for candidate in context if candidate.lane is Lane.Kind.FACTS]
    assert reranked and len(reranked[0]) >= 2
    assert fact_lines[0] == f"- ({ontology.RELATED_TO}) second seeded"
    assert sorted(counts.values()) == [1, 1]


def test_reordered_puts_scored_merit_first_and_unscored_in_arrival_order() -> None:
    identities = sorted(uuid.uuid4() for _ in range(5))
    facts = [
        Candidate(lane=Lane.Kind.FACTS, line=f"fact {index}", evidence_id=identity)
        for index, identity in enumerate(identities[:4])
    ]
    source = Candidate(lane=Lane.Kind.SOURCES, line="source", evidence_id=identities[4])

    scored = rescore_module.reordered(
        [*facts, source], {facts[0].evidence_id: 0.1, source.evidence_id: 0.9}
    )
    tied = rescore_module.reordered(
        [*facts, source], {facts[0].evidence_id: 0.5, facts[2].evidence_id: 0.5}
    )

    # Merit crosses lanes: the high-scoring source outranks every fact, scored beats
    # unscored, and the unscored tail keeps the statement's arrival order.
    assert [candidate.line for candidate in scored] == [
        "source",
        "fact 0",
        "fact 1",
        "fact 2",
        "fact 3",
    ]
    # Equal scores tie-break on evidence_id, exactly the statement's arrival order.
    assert [candidate.line for candidate in tied] == [
        "fact 0",
        "fact 2",
        "fact 1",
        "fact 3",
        "source",
    ]


def test_query_entities_reads_the_lowered_gate_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def named(text: str) -> list[str]:
        assert text == "who uses what"
        return ["ada", "git"]

    monkeypatch.setattr(recall_module, "named_entities", named)

    assert dbutil.run(recall_module.query_entities("who uses what")) == ["ada", "git"]


def test_query_entities_seeding_off_skips_the_gate_call_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def named(text: str) -> list[str]:
        raise AssertionError("the gate must not be called with seeding off")

    monkeypatch.setattr(recall_module, "named_entities", named)
    monkeypatch.setattr(settings, "graph_entity_seeding", False)

    assert dbutil.run(recall_module.query_entities("who uses what")) == []


def test_recall_runs_the_maximal_plan_unless_the_caller_forces_one(
    owner: uuid.UUID,
    fake_embedder: RecordingEmbedder,
    fake_reranker: list[list[str]],
    stub_entities: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User.private(owner)
    vector = deterministic_vector("query:what holds", settings.embed_dim)
    executed: list[Plan] = []

    def spying_build(context: QueryContext, plan: Plan) -> Select:
        executed.append(plan)
        return build_recall_statement(context, plan)

    monkeypatch.setattr(recall_module, "build_recall_statement", spying_build)

    async def probe() -> tuple[Candidate, ...]:
        await seed_fact(user, "the forced answer", vector)
        forced = await recall("what holds", user=user, token_budget=200, plan=Plan.focused())
        assert any("the forced answer" in candidate.line for candidate in forced)
        return await recall("what holds", user=user, token_budget=200)

    candidates = dbutil.run(probe())

    assert any("the forced answer" in candidate.line for candidate in candidates)
    assert executed == [Plan.focused(), Plan.maximal()]


def test_recall_embeds_before_its_single_context_query(
    owner: uuid.UUID,
    fake_embedder: RecordingEmbedder,
    fake_reranker: list[list[str]],
    stub_entities: None,
) -> None:
    user = User.authorized(owner, read=(owner,), write=(owner,), label="Pedro")
    search_query = "what holds\nThe asking speaker is Pedro."
    vector = deterministic_vector(f"query:{search_query}", settings.embed_dim)

    async def probe() -> tuple[Candidate, ...]:
        await seed_fact(user, "the remembered answer", vector)
        return await recall("what holds", user=user, token_budget=200)

    candidates = dbutil.run(probe())

    assert any("the remembered answer" in candidate.line for candidate in candidates)
    assert fake_embedder.calls == [([search_query], "query")]
    assert fake_reranker, "the cross-encoder scores every recall"
