from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace

import dbutil
import pytest
from doubles import RecordingEmbedder, deterministic_vector
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5, uuid5s, uuid7, uuid8
from pydantic import UUID5, UUID7
from sqlalchemy import Row, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.sql.selectable import Select
from sqlmodel import select

from aizk.config import settings
from aizk.config.settings import Settings
from aizk.ontology import Ontology, System
from aizk.retrieval import (
    Candidate,
    Lane,
    Plan,
    QueryContext,
    recall,
    trace,
)
from aizk.retrieval.lanes import FactLane, VectorLane
from aizk.retrieval.recall import build_recall_statement
from aizk.serving.gate import GateClient
from aizk.serving.rerank import RerankClient
from aizk.store import (
    Chunk,
    Document,
    Entity,
    Fact,
)
from aizk.store.identity import User

recall_module = import_module("aizk.retrieval.recall.orchestrator")
rescore_module = import_module("aizk.retrieval.rerank.rescore")


@pytest.fixture
def owner(migrated_db: None) -> Iterator[UUID5 | UUID7]:
    owner_id = uuid5()
    dbutil.run(dbutil.reset_db())
    yield owner_id
    dbutil.run(dbutil.reset_db())


def stub_gate(
    monkeypatch: pytest.MonkeyPatch, named_entities: Callable[[str], Awaitable[list[str]]]
) -> None:
    """Route the entity gate recall makes at the client seam, keeping tests hermetic."""
    monkeypatch.setattr(
        GateClient,
        "from_settings",
        classmethod(lambda cls, config: SimpleNamespace(named_entities=named_entities)),
    )


@pytest.fixture
def stub_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the entity gate call recall makes, since the client assumes a live sidecar."""

    async def no_entities(text: str) -> list[str]:
        del text
        return []

    stub_gate(monkeypatch, no_entities)


def basis(primary: float = 1.0, secondary: float = 0.0) -> list[float]:
    vector = [0.0] * settings.embed_dim
    vector[0], vector[1] = primary, secondary
    return vector


async def seed_fact(
    user: User,
    statement: str,
    vector: list[float],
) -> UUID5 | UUID7:
    entity = Entity.Content(id=uuid5(), name=f"entity {uuid7()}", type=System.Entity.CONCEPT)
    content = Fact.Content(
        id=uuid5(),
        subject_id=entity.id,
        predicate=System.Relation.RELATED_TO,
        statement=statement,
        embedding=vector,
    )
    claim = Fact.Claim(
        content_id=content.id,
        created_by=user.id,
        scopes=sorted(user.scopes.write),
    )
    async with User.system().owner as opened:
        opened.add(entity)
        await opened.flush()
        opened.add(
            Entity.Claim(
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
    query: str = "query",
) -> Sequence[Row]:
    statement = statement_for(len(vector), plan if plan is not None else Plan.focused())
    params = {
        "qvec": vector,
        "qtext": query,
        "qentities": entities or [],
        "k": lane_k,
        **settings.for_statement(statement),
    }
    async with user as session:
        result = await session.exec(statement, params=params)
        return result.all()


@given(lines=st.lists(st.text(min_size=1, max_size=40), max_size=8), identity=uuid5s)
def test_candidates_preserve_typed_identities_and_round_trip_as_immutable_values(
    lines: list[str], identity: UUID5
) -> None:
    candidates = tuple(Candidate(lane=Lane.Kind.FACTS, line=line) for line in lines)

    assert all(
        Candidate.model_validate_json(candidate.model_dump_json()) == candidate
        for candidate in candidates
    )
    assert (
        Candidate(lane=Lane.Kind.OVERVIEW, line="graph summary", evidence_id=identity).evidence_id
        == identity
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
def test_plan_presets_declare_and_compile_the_composed_query(
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


def test_vector_lane_rejects_a_section_it_does_not_serve() -> None:
    lane = VectorLane(kind=Lane.Kind.FACTS, priority=0)
    context = QueryContext(dimensions=settings.embed_dim, fuzzy=False)

    with pytest.raises(ValueError, match="not a vector-only section"):
        lane(context)


@pytest.mark.parametrize("preset", [Plan.focused, Plan.overview, Plan.multihop, Plan.maximal])
def test_context_query_executes_every_preset_on_an_empty_graph(
    owner: UUID5 | UUID7, preset: Callable[[], Plan]
) -> None:
    user = User.private(owner)
    assert dbutil.run(retrieve(user, basis(), preset())) == []


def test_recall_packs_within_budget_and_records_only_the_kept_fact(
    owner: UUID5 | UUID7,
    fake_embedder: RecordingEmbedder,
    fake_reranker: list[list[str]],
    stub_entities: None,
) -> None:
    user = User.private(owner)
    vector = deterministic_vector("query:what holds", settings.embed_dim)

    async def probe() -> tuple[list[Candidate], dict[UUID5 | UUID7, int]]:
        # The small fact arrives first, so the prefix cut keeps it and stops at the
        # large one that no longer fits.
        small = await seed_fact(user, "short", vector)
        large = await seed_fact(user, "x" * 1000, vector)
        context = await recall("what holds", user=user, token_budget=20)
        async with User.system().owner as opened:
            counts = dict(
                (
                    await opened.exec(
                        select(Fact.Claim.id, Fact.Claim.access_count).where(
                            Fact.Claim.id.in_([large, small])
                        )
                    )
                ).all()
            )
        return context, counts

    context, counts = dbutil.run(probe())

    assert [candidate.line for candidate in context] == ["- (related_to) short"]
    assert sorted(counts.values()) == [0, 1]


def test_context_query_relies_on_rls_for_the_complete_visibility_boundary(
    owner: UUID5 | UUID7,
) -> None:
    visible = User.private(owner)
    hidden = User.private(uuid5())

    async def probe() -> list[str]:
        await seed_fact(visible, "visible fact", basis())
        await seed_fact(hidden, "hidden fact", basis())
        return [row._mapping["line"] for row in await retrieve(visible, basis())]

    lines = dbutil.run(probe())

    assert any("visible fact" in line for line in lines)
    assert all("hidden fact" not in line for line in lines)


def test_fact_lane_deduplicates_identical_world_statements(owner: UUID5 | UUID7) -> None:
    user = User.private(owner)

    async def probe() -> list[str]:
        await seed_fact(user, "one shared statement", basis())
        await seed_fact(user, "one shared statement", basis())
        return [row._mapping["line"] for row in await retrieve(user, basis())]

    lines = dbutil.run(probe())

    assert lines.count("- (related_to) one shared statement") == 1


def test_source_lane_preserves_a_complete_recall_sized_note(owner: UUID5 | UUID7) -> None:
    user = User.private(owner)
    text = "Current projects. " + "relevant detail " * 110 + "Final verified project."

    async def probe() -> list[str]:
        document = Document(
            id=uuid7(),
            content_hash=uuid8(),
            created_by=user.id,
            scopes=[user.id],
            title="Current projects",
        )
        async with user as session:
            session.add(document)
            await session.flush()
            session.add(
                Chunk(
                    document_id=document.id,
                    ord=0,
                    text=text,
                    embedding=basis(),
                    created_by=user.id,
                    scopes=[user.id],
                )
            )
        return [row._mapping["line"] for row in await retrieve(user, basis())]

    lines = dbutil.run(probe())

    source = next(line for line in lines if line.startswith("Current projects"))
    assert "Final verified project." in source


def test_source_lane_guarantees_a_document_whose_complete_title_is_in_the_query(
    owner: UUID5 | UUID7,
) -> None:
    user = User.private(owner)

    async def add(title: str, text: str, embedding: list[float]) -> None:
        document = Document(
            id=uuid7(),
            content_hash=uuid8(),
            created_by=user.id,
            scopes=[user.id],
            title=title,
        )
        async with user as session:
            session.add(document)
            await session.flush()
            session.add(
                Chunk(
                    document_id=document.id,
                    ord=0,
                    text=text,
                    embedding=embedding,
                    created_by=user.id,
                    scopes=[user.id],
                )
            )

    async def probe() -> list[str]:
        await add("Japanese", "Daily grammar practice.", basis(0.0, 1.0))
        await add("Finances", "Japanese risks need attention.", basis())
        return [
            row._mapping["line"]
            for row in await retrieve(
                user,
                basis(),
                lane_k=1,
                query="What risks need attention in the Japanese Area?",
            )
        ]

    lines = dbutil.run(probe())

    source = next(line for line in lines if line.startswith("Japanese"))
    assert "Japanese" in source
    assert "Daily grammar practice." in source


def test_source_lane_excludes_expired_documents_and_labels_observation_time(
    owner: UUID5 | UUID7, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User.private(owner)
    monkeypatch.setattr(settings, "display_timezone", "Asia/Tokyo")
    observed = datetime(2026, 7, 14, 15, tzinfo=UTC)

    async def add(title: str, expires_at: datetime) -> None:
        document = Document(
            id=uuid7(),
            content_hash=uuid8(),
            created_by=user.id,
            scopes=[user.id],
            title=title,
            observed_at=observed,
            expires_at=expires_at,
        )
        async with user as session:
            session.add(document)
            await session.flush()
            session.add(
                Chunk(
                    document_id=document.id,
                    ord=0,
                    text=f"{title} evidence",
                    embedding=basis(),
                    created_by=user.id,
                    scopes=[user.id],
                )
            )

    async def probe() -> list[str]:
        await add("Expired", datetime.now(UTC) - timedelta(days=1))
        await add("Current", datetime.now(UTC) + timedelta(days=1))
        return [row._mapping["line"] for row in await retrieve(user, basis())]

    lines = dbutil.run(probe())

    assert all("Expired" not in line for line in lines)
    current = next(line for line in lines if "Current" in line)
    assert "observed 2026-07-15" in current


def test_entity_catalog_is_generic_and_preserves_exact_scopes(owner: UUID5 | UUID7) -> None:
    shared, hidden = uuid5(), uuid5()
    visible = User.authorized(owner, read=(owner, shared), write=(owner,))

    async def add(
        scope: UUID5 | UUID7,
        title: str,
        subject_type: str,
        state: str | None = None,
    ) -> None:
        subject_id = uuid5()
        async with User.private(scope) as session:
            session.add(Entity.Content(id=subject_id, name=title, type=subject_type))
            await session.flush()
            session.add(Entity.Claim(content_id=subject_id, created_by=scope, scopes=[scope]))
            session.add(
                Document(
                    id=uuid7(),
                    content_hash=uuid8(),
                    created_by=scope,
                    scopes=[scope],
                    title=title,
                    subject_type=subject_type,
                )
            )
            if state is None:
                return
            state_id = uuid5()
            session.add(Entity.Content(id=state_id, name=state, type="status"))
            await session.flush()
            session.add(Entity.Claim(content_id=state_id, created_by=scope, scopes=[scope]))
            content_id = uuid5()
            session.add(
                Fact.Content(
                    id=content_id,
                    subject_id=subject_id,
                    object_id=state_id,
                    predicate="has_status",
                    statement=f"{title} has status {state}.",
                )
            )
            await session.flush()
            session.add(Fact.Claim(content_id=content_id, created_by=scope, scopes=[scope]))

    async def probe() -> Sequence[Row]:
        async with User.system().owner as session:
            await session.exec(
                update(Entity.Kind)
                .where(Entity.Kind.name.in_(["project", "area", "status"]))
                .values(embedding=basis())
            )
        await add(owner, "Aizk", "project", "Active")
        await add(shared, "Aizk", "project", "Completed")
        await add(owner, "Research", "area")
        await add(hidden, "Hidden", "project", "Active")
        return await retrieve(visible, basis())

    rows = dbutil.run(probe())
    catalogs = [
        row._mapping for row in rows if str(row._mapping["source_title"]).endswith("catalog")
    ]

    assert {tuple(row["scopes"]) for row in catalogs} == {(owner,), (shared,)}
    lines = {row["line"] for row in catalogs}
    assert "Current area entities are Research." in lines
    assert "Current project entities are Aizk (Aizk has status Active.)." in lines
    assert "Current project entities are Aizk (Aizk has status Completed.)." in lines
    assert all("Hidden" not in line for line in lines)


def test_multihop_walk_reaches_beyond_the_local_neighbor_lane(owner: UUID5 | UUID7) -> None:
    user = User.private(owner)

    async def probe(plan: Plan) -> list[str]:
        entities = [
            Entity.Content(id=uuid5(), name=f"chain {index} {uuid7()}", type=System.Entity.CONCEPT)
            for index in range(4)
        ]
        async with User.system().owner as opened:
            opened.add_all(entities)
            await opened.flush()
            opened.add_all(
                Entity.Claim(
                    content_id=entity.id,
                    created_by=user.id,
                    scopes=sorted(user.scopes.write),
                )
                for entity in entities
            )
            contents = [
                Fact.Content(
                    id=uuid5(),
                    subject_id=subject.id,
                    object_id=target.id,
                    predicate=System.Relation.RELATED_TO,
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
                Fact.Claim(
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
    owner: UUID5 | UUID7,
) -> None:
    user = User.private(owner)
    now = datetime.now(UTC)

    async def probe() -> list[str]:
        entity = Entity.Content(id=uuid5(), name=f"subject {uuid7()}", type=System.Entity.CONCEPT)
        async with User.system().owner as opened:
            opened.add(entity)
            await opened.flush()
            opened.add(Entity.Claim(content_id=entity.id, created_by=user.id, scopes=[user.id]))
            for label, recorded, accessed, count in (
                ("stale fact", now - timedelta(days=200), None, 0),
                ("fresh fact", now - timedelta(days=1), now, 12),
            ):
                content = Fact.Content(
                    id=uuid5(),
                    subject_id=entity.id,
                    predicate=System.Relation.RELATED_TO,
                    statement=label,
                    embedding=basis(),
                )
                opened.add(content)
                await opened.flush()
                claim = Fact.Claim(
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
    owner: UUID5 | UUID7,
) -> None:
    user = User.private(owner)
    far = basis(-1.0, 0.0)

    async def seed_chain() -> None:
        entities = [
            Entity.Content(id=uuid5(), name=f"hop {index}", type=System.Entity.CONCEPT)
            for index in range(6)
        ]
        async with User.system().owner as opened:
            opened.add_all(entities)
            await opened.flush()
            opened.add_all(
                Entity.Claim(content_id=entity.id, created_by=user.id, scopes=[user.id])
                for entity in entities
            )
            contents = [
                Fact.Content(
                    id=uuid5(),
                    subject_id=entities[index].id,
                    object_id=entities[index + 1].id,
                    predicate=System.Relation.RELATED_TO,
                    statement=f"edge {index}",
                    embedding=basis() if index == 0 else far,
                )
                for index in range(5)
            ]
            opened.add_all(contents)
            await opened.flush()
            opened.add_all(
                Fact.Claim(content_id=content.id, created_by=user.id, scopes=[user.id])
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
    owner: UUID5 | UUID7,
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

    monkeypatch.setattr(
        RerankClient,
        "from_settings",
        classmethod(lambda cls, config: SimpleNamespace(rerank=rerank)),
    )

    async def probe() -> tuple[list[Candidate], dict[UUID5 | UUID7, int]]:
        first = await seed_fact(user, "first seeded", vector)
        second = await seed_fact(user, "second seeded", vector)
        context = await recall("what holds", user=user, token_budget=400)
        async with User.system().owner as opened:
            counts = dict(
                (
                    await opened.exec(
                        select(Fact.Claim.id, Fact.Claim.access_count).where(
                            Fact.Claim.id.in_([first, second])
                        )
                    )
                ).all()
            )
        return context, counts

    context, counts = dbutil.run(probe())

    fact_lines = [candidate.line for candidate in context if candidate.lane is Lane.Kind.FACTS]
    assert reranked and len(reranked[0]) >= 2
    assert fact_lines[0] == f"- ({System.Relation.RELATED_TO}) second seeded"
    assert sorted(counts.values()) == [1, 1]


def test_recall_trace_preserves_access_history(
    owner: UUID5 | UUID7,
    fake_embedder: RecordingEmbedder,
    fake_reranker: list[list[str]],
    stub_entities: None,
) -> None:
    user = User.private(owner)
    vector = deterministic_vector("query:what holds", settings.embed_dim)

    async def probe() -> tuple[int, int]:
        fact_id = await seed_fact(user, "diagnostic fact", vector)
        diagnostic = await trace("what holds", user=user, token_budget=400)
        async with User.system().owner as opened:
            count = await opened.scalar(
                select(Fact.Claim.access_count).where(Fact.Claim.id == fact_id)
            )
        return diagnostic.selected, count

    selected, access_count = dbutil.run(probe())

    assert selected > 0
    assert access_count == 0
    assert fake_reranker


@given(
    score_states=st.lists(
        st.none() | st.integers(min_value=0, max_value=4), min_size=1, max_size=8
    )
)
def test_reordered_sorts_scored_merit_and_preserves_the_unscored_tail(
    score_states: list[int | None],
) -> None:
    identities = sorted(uuid7() for _ in score_states)
    candidates = [
        Candidate(lane=Lane.Kind.FACTS, line=f"fact {index}", evidence_id=identity)
        for index, identity in enumerate(identities)
    ]
    scores = {
        candidate.evidence_id: float(score)
        for candidate, score in zip(candidates, score_states, strict=True)
        if score is not None
    }

    ordered = rescore_module.reordered(candidates, scores)
    scored = [candidate for candidate in ordered if candidate.evidence_id in scores]
    unscored = [candidate for candidate in ordered if candidate.evidence_id not in scores]

    assert scored == sorted(
        (candidate for candidate in candidates if candidate.evidence_id in scores),
        key=lambda candidate: (-scores[candidate.evidence_id], candidate.evidence_id),
    )
    assert unscored == [
        candidate for candidate in candidates if candidate.evidence_id not in scores
    ]


@pytest.mark.parametrize(
    ("descriptors", "expected"),
    [
        (
            [
                (Lane.Kind.FACTS, "fact 0", None, False, 0.1),
                (Lane.Kind.FACTS, "fact 1", None, False, None),
                (Lane.Kind.FACTS, "fact 2", None, False, None),
                (Lane.Kind.FACTS, "fact 3", None, False, None),
                (Lane.Kind.SOURCES, "source", None, False, 0.9),
            ],
            ["source", "fact 0", "fact 1", "fact 2", "fact 3"],
        ),
        (
            [
                (Lane.Kind.SOURCES, "incidental mention", None, False, 0.99),
                (Lane.Kind.SOURCES, "named source", None, True, 0.01),
            ],
            ["named source", "incidental mention"],
        ),
        (
            [
                (Lane.Kind.SOURCES, "short title", "JLPT N2", True, 0.99),
                (
                    Lane.Kind.SOURCES,
                    "maximal title",
                    "JLPT N2 Window Weekly Plan",
                    True,
                    0.01,
                ),
                (Lane.Kind.SOURCES, "unrelated named title", "Japanese", True, 0.02),
            ],
            ["unrelated named title", "maximal title", "short title"],
        ),
    ],
    ids=["cross-lane-merit", "direct-over-merit", "maximal-title"],
)
def test_reordered_preserves_merit_and_named_source_authority(
    descriptors: list[tuple[Lane.Kind, str, str | None, bool, float | None]],
    expected: list[str],
) -> None:
    identities = sorted(uuid7() for _ in descriptors)
    candidates = [
        Candidate(
            lane=lane,
            line=line,
            source_title=title,
            evidence_id=identity,
            direct=direct,
        )
        for identity, (lane, line, title, direct, _) in zip(identities, descriptors, strict=True)
    ]
    scores = {
        candidate.evidence_id: score
        for candidate, (*_, score) in zip(candidates, descriptors, strict=True)
        if score is not None
    }

    assert [
        candidate.line for candidate in rescore_module.reordered(candidates, scores)
    ] == expected


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [(True, ["ada", "git"]), (False, [])],
    ids=["enabled", "disabled"],
)
def test_query_entity_seeding_controls_the_lowered_gate_names(
    enabled: bool, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def named(text: str) -> list[str]:
        calls.append(text)
        assert text == "who uses what"
        return ["ada", "git"]

    stub_gate(monkeypatch, named)
    monkeypatch.setattr(settings, "graph_entity_seeding", enabled)

    assert dbutil.run(recall_module.query_entities("who uses what", User.system())) == expected
    assert calls == (["who uses what"] if enabled else [])


def test_query_entities_loads_the_ontology_in_a_fresh_process(
    migrated_db: None,
    fake_embedder: RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def named(text: str) -> list[str]:
        assert Ontology.current().entity_names
        return [text]

    monkeypatch.setattr(Ontology, "_cached", None)
    stub_gate(monkeypatch, named)

    assert dbutil.run(recall_module.query_entities("ada", User.system())) == ["ada"]


def test_recall_runs_the_maximal_plan_unless_the_caller_forces_one(
    owner: UUID5 | UUID7,
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

    async def probe() -> list[Candidate]:
        await seed_fact(user, "the forced answer", vector)
        forced = await recall("what holds", user=user, token_budget=200, plan=Plan.focused())
        assert any("the forced answer" in candidate.line for candidate in forced)
        return await recall("what holds", user=user, token_budget=200)

    candidates = dbutil.run(probe())

    assert any("the forced answer" in candidate.line for candidate in candidates)
    assert executed == [Plan.focused(), Plan.maximal()]


def test_recall_embeds_before_its_single_context_query(
    owner: UUID5 | UUID7,
    fake_embedder: RecordingEmbedder,
    fake_reranker: list[list[str]],
    stub_entities: None,
) -> None:
    user = User.authorized(owner, read=(owner,), write=(owner,), label="Pedro")
    search_query = "what holds\nThe asking speaker is Pedro."
    vector = deterministic_vector(f"query:{search_query}", settings.embed_dim)

    async def probe() -> list[Candidate]:
        await seed_fact(user, "the remembered answer", vector)
        return await recall("what holds", user=user, token_budget=200)

    candidates = dbutil.run(probe())

    assert any("the remembered answer" in candidate.line for candidate in candidates)
    assert fake_embedder.calls == [([search_query], "query")]
    assert fake_reranker, "the cross-encoder scores every recall"
