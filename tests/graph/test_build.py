import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NoReturn, cast

import dbutil
import httpx
import pytest
import seedgraph
from asyncpg.exceptions import TransactionRollbackError
from doubles import FakeLLM, RecordingEmbedder, deterministic_vector
from id_factory import uuid5, uuid7, uuid8
from openai import APIConnectionError, APITimeoutError
from pydantic import UUID5, UUID7, ValidationError
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import select

import aizk.graph.build as build
import aizk.serving.extract.client as llm_module
from aizk.config import settings
from aizk.extract.models import (
    BatchConsolidationVerdict,
    ConsolidationVerdict,
    ExtractedEntity,
    Extraction,
    TimedFact,
)
from aizk.graph.build import (
    build_graph,
    is_transient_db_error,
    prepare_entities,
    raise_failures,
    source_extraction,
    write_graph_slice,
)
from aizk.graph.consolidation import FactMatch
from aizk.graph.ids import entity_id, fact_id
from aizk.graph.repair import dedup_entities, redirect_entity
from aizk.graph.writer import FactCandidate, FactPlan, GraphWriter, PreparedEntity
from aizk.ontology import Ontology, System, WireEntity, WireExtraction, WireFact
from aizk.ontology import catalog as ontology_catalog
from aizk.provenance import CaptureContext, EpistemicKind
from aizk.serving.embed import EmbedMode
from aizk.store import (
    Chunk,
    Document,
    Entity,
    Fact,
)
from aizk.store.identity import User

if TYPE_CHECKING:
    from aizk.store.engine import Session

pytestmark = pytest.mark.usefixtures("migrated_db")

# Long enough to enter the model extraction path
LONG_PROSE = "Ada Lovelace keeps detailed notes about memory and computation across her notebooks."
GATE_OFF = {settings.skip_live_gate: True}

# Unit vectors place candidates at the borderline and automatic merge thresholds.
E0 = [1.0] + [0.0] * (settings.embed_dim - 1)
E_BAND = [0.8, 0.6] + [0.0] * (settings.embed_dim - 2)


class FixedEmbedder(RecordingEmbedder):
    def __init__(self, vector: list[float]) -> None:
        super().__init__()
        self.vector = vector

    async def embed(self, texts: list[str], mode: EmbedMode = "document") -> list[list[float]]:
        self.calls.append((list(texts), mode))
        return [list(self.vector) for _ in texts]


async def resolve(writer: GraphWriter, name: str, type: str) -> UUID5 | UUID7 | None:
    [vector] = await build.embed([name], mode="document")
    return await writer.resolve(PreparedEntity(name=name, type=type, vector=tuple(vector)))


async def consolidate(
    writer: GraphWriter,
    facts: list[TimedFact],
    resolved: dict[str, UUID5 | UUID7],
    chunk: UUID5 | UUID7,
) -> None:
    candidates = await writer.new_candidates(facts, resolved)
    vectors = await build.embed(
        [candidate.fact.statement for candidate in candidates], mode="document"
    )
    plans = await writer.plan_facts(candidates, vectors)
    decisions = await writer.resolve_ambiguous(plans)
    await writer.apply_plans(plans, decisions, chunk)


class FakeGate:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[str] = []

    async def relevant(self, text: str) -> bool:
        self.calls.append(text)
        return self.result


@pytest.fixture
def fake_gate(monkeypatch: pytest.MonkeyPatch) -> FakeGate:
    gate = FakeGate()
    monkeypatch.setattr(build, "relevant", gate.relevant)
    return gate


@pytest.fixture
def fixed_embedder(monkeypatch: pytest.MonkeyPatch) -> FixedEmbedder:
    embedder = FixedEmbedder(E0)
    monkeypatch.setattr(build, "embed", embedder.embed)
    monkeypatch.setattr(ontology_catalog, "embed", embedder.embed)
    return embedder


def install_raising_client(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    llm = FakeLLM()
    llm.completions.error = error
    monkeypatch.setattr(llm_module, "llm_model", lambda *args: llm.model)


def test_build_graph_loads_ontology_for_a_fresh_process(
    monkeypatch: pytest.MonkeyPatch, fixed_embedder: FixedEmbedder
) -> None:
    monkeypatch.setattr(Ontology, "_cached", None)

    assert dbutil.run(build_graph(limit=0)) == (0, 0)
    assert Ontology.current().entity_names


class WrappedDBAPI(Exception):
    def __init__(self, inner: BaseException) -> None:
        self.orig = inner


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DBAPIError("s", None, WrappedDBAPI(TransactionRollbackError("deadlock"))), True),
        (DBAPIError("s", None, WrappedDBAPI(ValueError("other"))), False),
        (ValueError("not a db error"), False),
    ],
    ids=["rollback", "other-dbapi", "non-dbapi"],
)
def test_is_transient_db_error(error: BaseException, expected: bool) -> None:
    assert is_transient_db_error(error) is expected


def test_redirect_entity_resolves_null_absent_replaced_and_dropped() -> None:
    canonical, duplicate, dropped, untouched = (uuid5() for _ in range(4))
    redirect = {duplicate: canonical, dropped: None}
    assert redirect_entity(redirect, None) == (None, False)
    assert redirect_entity(redirect, untouched) == (untouched, False)
    assert redirect_entity(redirect, duplicate) == (canonical, False)
    assert redirect_entity(redirect, dropped) == (None, True)


@pytest.mark.parametrize("scenario", ["insert", "exact", "fuzzy", "path"])
def test_resolve_mints_reuses_folds_or_drops(
    scenario: str, fake_embedder: RecordingEmbedder
) -> None:
    async def body() -> UUID5 | UUID7 | None:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            if scenario == "exact":
                await seedgraph.add_entity(
                    session,
                    owner,
                    "Exact Fixture",
                    type="author",
                    content_id=entity_id("Exact Fixture", "author"),
                )
            if scenario == "fuzzy":
                await seedgraph.add_entity(
                    session,
                    owner,
                    "Existing",
                    type="concept",
                    embedding=deterministic("document:Newcomer"),
                    content_id=entity_id("Existing", "concept"),
                )
        async with dbutil.actor(owner) as session:
            writer = GraphWriter(session, owner, frozenset({owner}))
            if scenario == "insert":
                first = await resolve(writer, "Brand New", "concept")
                second = await resolve(writer, "Brand New", "concept")
                assert first == second  # the second resolve reuses off the minted claim
                return first
            if scenario == "exact":
                return await resolve(writer, "Exact Fixture", "author")
            if scenario == "fuzzy":
                return await resolve(writer, "Newcomer", "concept")
            return await resolve(writer, "notes/graph_rag.md", "concept")

    expected = {
        "insert": entity_id("Brand New", "concept"),
        "exact": entity_id("Exact Fixture", "author"),
        "fuzzy": entity_id("Existing", "concept"),
        "path": None,
    }
    assert dbutil.run(body()) == expected[scenario]


def test_nonstructural_entities_can_fuzzy_fold(fixed_embedder: FixedEmbedder) -> None:
    async def body() -> tuple[UUID5 | UUID7 | None, UUID5 | UUID7 | None]:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            writer = GraphWriter(session, owner, frozenset({owner}))
            research = await resolve(writer, "Research", "area")
            business = await resolve(writer, "Business", "area")
        return research, business

    research, business = dbutil.run(body())
    assert research == entity_id("Research", "area")
    assert business == research


def test_exact_names_with_distinct_types_remain_distinct_entities(
    fake_embedder: RecordingEmbedder,
) -> None:
    async def body() -> tuple[UUID5 | UUID7, UUID5 | UUID7 | None]:
        owner = await seedgraph.fresh_owner()
        structural = entity_id("Research", "area")
        async with dbutil.actor(owner) as session:
            await seedgraph.add_entity(
                session,
                owner,
                "Research",
                type="area",
                content_id=structural,
            )
        async with dbutil.actor(owner) as session:
            resolved = await resolve(
                GraphWriter(session, owner, frozenset({owner})),
                "research",
                System.Entity.CONCEPT,
            )
        return structural, resolved

    structural, resolved = dbutil.run(body())
    assert resolved == entity_id("research", System.Entity.CONCEPT)
    assert resolved != structural


def test_batch_mint_reraises_a_non_unique_integrity_error() -> None:
    async def body() -> None:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            with pytest.raises(IntegrityError):
                await Entity.Content.mint_all(
                    session,
                    [Entity.Content(id=uuid5(), name="invalid", type="missing ontology kind")],
                )

    dbutil.run(body())


def deterministic(text: str) -> list[float]:
    return deterministic_vector(text, settings.embed_dim)


@pytest.mark.parametrize("scenario", ["add", "noop", "update"])
def test_consolidate_applies_verdict(scenario: str, fixed_embedder: FixedEmbedder) -> None:
    now = datetime.now(UTC)

    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
            resolved = {"Subject": subject}
            if scenario in {"noop", "update"}:
                target = (
                    await seedgraph.add_entity(session, owner, "Obj One")
                    if scenario == "update"
                    else None
                )
                await seedgraph.add_fact(
                    session,
                    owner,
                    subject,
                    statement="seeded fact",
                    predicate="has_status",
                    object_id=target,
                    embedding=E0,
                    valid=Range(now, None) if scenario == "update" else None,
                )
            if scenario == "update":
                resolved["Obj Two"] = await seedgraph.add_entity(session, owner, "Obj Two")
        fact = TimedFact(
            subject="Subject",
            predicate="has_status",
            object="Obj Two" if scenario == "update" else "",
            statement="candidate fact",
            valid_from=now - timedelta(days=10) if scenario == "update" else None,
        )
        async with dbutil.actor(owner) as session:
            await consolidate(
                GraphWriter(session, owner, frozenset({owner})), [fact], resolved, chunk
            )
        async with dbutil.actor(owner) as session:
            total = (
                await session.exec(
                    select(func.count()).select_from(Fact.Claim).execution_options(**GATE_OFF)
                )
            ).one()
            live = (
                await session.exec(
                    select(func.count())
                    .select_from(Fact.Live)
                    .where(Fact.Live.subject_id == subject)
                )
            ).one()
        return total or 0, live or 0

    assert dbutil.run(body()) == {"add": (1, 1), "noop": (1, 1), "update": (2, 1)}[scenario]


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ("detailed  NOTES about memory", (19, 46)),
        ("never said anywhere", None),
        (None, None),
    ],
    ids=["mangled-aligns", "unfindable", "absent"],
)
def test_fact_claims_carry_quote_offsets_when_the_quote_aligns(
    quote: str | None,
    expected: tuple[int, int] | None,
    fixed_embedder: FixedEmbedder,
) -> None:
    async def body() -> dict:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
        fact = TimedFact(subject="Subject", predicate="uses", statement="grounded", quote=quote)
        async with dbutil.actor(owner) as session:
            writer = GraphWriter(session, owner, frozenset({owner}), source_text=LONG_PROSE)
            await consolidate(writer, [fact], {"Subject": subject}, chunk)
        async with dbutil.actor(owner) as session:
            return (
                await session.exec(
                    select(Fact.Claim.attributes)
                    .where(Fact.Claim.source_chunk_id == chunk)
                    .execution_options(**GATE_OFF)
                )
            ).one()

    attributes = dbutil.run(body())

    if expected is None:
        assert "quote_start" not in attributes and "quote_end" not in attributes
    else:
        assert (attributes["quote_start"], attributes["quote_end"]) == expected
        assert LONG_PROSE[attributes["quote_start"] : attributes["quote_end"]] == (
            "detailed notes about memory"
        )


def test_consolidate_is_idempotent_and_reads_an_empty_pool(fixed_embedder: FixedEmbedder) -> None:
    async def body() -> tuple[list[FactPlan], int]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
        fact = TimedFact(subject="Subject", predicate="uses", statement="only")
        async with dbutil.actor(owner) as session:
            writer = GraphWriter(session, owner, frozenset({owner}))
            empty = await writer.plan_facts([], [])
            await consolidate(writer, [fact], {"Subject": subject}, chunk)
        async with dbutil.actor(owner) as session:
            await consolidate(
                GraphWriter(session, owner, frozenset({owner})),
                [fact],
                {"Subject": subject},
                chunk,
            )
        async with dbutil.actor(owner) as session:
            total = (
                await session.exec(
                    select(func.count()).select_from(Fact.Claim).execution_options(**GATE_OFF)
                )
            ).one()
        return empty, total or 0

    empty, total = dbutil.run(body())
    assert empty == []  # no candidates means no ranking query
    assert total == 1  # the second consolidation added no second claim


def test_concurrent_slices_revalidate_one_logical_fact_slot(
    fixed_embedder: FixedEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_plan = GraphWriter.plan_facts
    first_plans: set[asyncio.Task] = set()
    both_planned = asyncio.Event()

    async def synchronized_plan(
        writer: GraphWriter,
        candidates: list[FactCandidate],
        vectors: list[list[float]],
    ) -> list[FactPlan]:
        plans = await real_plan(writer, candidates, vectors)
        task = asyncio.current_task()
        assert task is not None
        if task not in first_plans:
            first_plans.add(task)
            if len(first_plans) == 2:
                both_planned.set()
            await both_planned.wait()
        return plans

    monkeypatch.setattr(GraphWriter, "plan_facts", synchronized_plan)

    async def body() -> int:
        owner = await seedgraph.fresh_owner()
        chunk_ids = [
            await seedgraph.seed_chunk(owner, f"{LONG_PROSE} version {index}")
            for index in range(2)
        ]
        facts = [
            TimedFact(
                subject="Subject",
                predicate="part_of",
                statement=f"Subject carries concurrent state {index}",
            )
            for index in range(2)
        ]

        async def write(chunk_id: UUID5 | UUID7, fact: TimedFact) -> None:
            async with dbutil.actor(owner).session() as opened:
                async with opened.begin():
                    chunk = await opened.get(Chunk, chunk_id)
                assert chunk is not None
                await write_graph_slice(
                    opened,
                    chunk,
                    [ExtractedEntity(name="Subject", type="concept")],
                    [fact],
                )

        await asyncio.gather(
            *(write(chunk_id, fact) for chunk_id, fact in zip(chunk_ids, facts, strict=True))
        )
        async with dbutil.actor(owner) as session:
            return (await session.exec(select(func.count()).select_from(Fact.Live))).one()

    assert dbutil.run(body()) == 1


def test_speaker_bound_claims_coexist_inside_one_shared_scope(
    fixed_embedder: FixedEmbedder,
) -> None:
    alice, bob = uuid5(), uuid5()

    async def body() -> list[tuple[str, str | None]]:
        scope = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(scope, LONG_PROSE)
        async with dbutil.actor(scope) as session:
            subject = await seedgraph.add_entity(session, scope, "Subject")
        opinion = TimedFact(
            subject="Subject",
            predicate="uses",
            statement="The plan looks risky.",
            kind=EpistemicKind.opinion,
        )
        for speaker, label in ((alice, "Alice"), (bob, "Bob")):
            async with dbutil.actor(scope) as session:
                writer = GraphWriter(
                    session,
                    speaker,
                    frozenset({scope}),
                    CaptureContext(speaker_label=label, speaker_role="Analyst"),
                )
                await consolidate(writer, [opinion], {"Subject": subject}, chunk)
        async with dbutil.actor(scope) as session:
            rows = await session.exec(
                select(Fact.Live.perspective_key, Fact.Live.attributes).where(
                    Fact.Live.subject_id == subject
                )
            )
            return sorted(
                (perspective, attributes.get("speaker_label")) for perspective, attributes in rows
            )

    assert dbutil.run(body()) == sorted(
        [(f"speaker:{speaker}", label) for speaker, label in ((alice, "Alice"), (bob, "Bob"))]
    )


def test_consolidate_defers_borderline_facts_to_the_batch(
    fixed_embedder: FixedEmbedder, fake_llm: FakeLLM
) -> None:
    phantom = uuid7()

    async def body() -> int:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
            await seedgraph.add_fact(
                session,
                owner,
                subject,
                statement="seeded",
                predicate="part_of",
                embedding=E_BAND,
            )
        fake_llm.register(
            BatchConsolidationVerdict,
            BatchConsolidationVerdict(
                verdicts=[
                    ConsolidationVerdict(action="UPDATE", supersedes=phantom),
                    ConsolidationVerdict(action="ADD"),
                ]
            ),
        )
        facts = [
            TimedFact(subject="Subject", predicate="part_of", statement=text)
            for text in ("first candidate", "second candidate")
        ]
        async with dbutil.actor(owner) as session:
            await consolidate(
                GraphWriter(session, owner, frozenset({owner})), facts, {"Subject": subject}, chunk
            )
        async with dbutil.actor(owner) as session:
            return (
                await session.exec(
                    select(func.count()).select_from(Fact.Claim).execution_options(**GATE_OFF)
                )
            ).one()

    assert dbutil.run(body()) == 3  # one seeded plus the two borderline candidates


@pytest.mark.parametrize("bounded", [False, True], ids=["open", "bounded"])
def test_backdated_update_becomes_history_without_retiring_newer_state(bounded: bool) -> None:
    newer_start = datetime.now(UTC)
    older_start = newer_start - timedelta(days=10)
    older_end = older_start + timedelta(days=1) if bounded else None

    async def body() -> dict[str, tuple[bool, datetime | None, datetime | None]]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
            current_object = await seedgraph.add_entity(session, owner, "Current")
            older_object = await seedgraph.add_entity(session, owner, "Older")
            _, current_claim = await seedgraph.add_fact(
                session,
                owner,
                subject,
                statement="the current state",
                object_id=current_object,
                embedding=E0,
                valid=Range(newer_start, None),
            )
        fact = TimedFact(
            subject="Subject",
            predicate="related_to",
            object="Older",
            statement="the older state",
            valid_from=older_start,
            valid_to=older_end,
        )
        async with dbutil.actor(owner) as session:
            candidate = FactCandidate(
                fact=fact,
                subject_id=subject,
                object_id=older_object,
                identity=fact_id(subject, fact.predicate, older_object, fact.statement),
            )
            await GraphWriter(session, owner, frozenset({owner})).apply_plans(
                [
                    FactPlan(
                        candidate=candidate,
                        vector=tuple(E0),
                        matches=(),
                        verdict=ConsolidationVerdict(action="UPDATE", supersedes=current_claim),
                    )
                ],
                [],
                chunk,
            )
        async with dbutil.actor(owner) as session:
            rows = await session.exec(
                select(Fact.Content.statement, Fact.Claim.recorded, Fact.Claim.valid)
                .join(Fact.Claim, Fact.Claim.content_id == Fact.Content.id)
                .execution_options(**GATE_OFF)
            )
            return {
                statement: (recorded.upper_inf, valid.lower, valid.upper)
                for statement, recorded, valid in rows
            }

    claims = dbutil.run(body())
    assert claims["the current state"] == (True, newer_start, None)
    assert claims["the older state"] == (True, older_start, older_end or newer_start)


def test_write_graph_slice_stops_after_four_stale_plans(
    fixed_embedder: FixedEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def changing_plan(
        writer: GraphWriter,
        candidates: list[FactCandidate],
        vectors: list[list[float]],
    ) -> list[FactPlan]:
        del writer
        return [
            FactPlan(
                candidate=candidate,
                vector=tuple(vector),
                matches=(
                    FactMatch(
                        id=uuid7(),
                        object_id=None,
                        statement="changing concurrent state",
                        distance=0.0,
                    ),
                ),
                verdict=ConsolidationVerdict(action="NOOP"),
            )
            for candidate, vector in zip(candidates, vectors, strict=True)
        ]

    monkeypatch.setattr(GraphWriter, "plan_facts", changing_plan)

    async def body() -> None:
        owner = await seedgraph.fresh_owner()
        chunk_id = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner).session() as opened:
            async with opened.begin():
                chunk = await opened.get(Chunk, chunk_id)
            assert chunk is not None
            with pytest.raises(RuntimeError, match="changed during four"):
                await write_graph_slice(
                    opened,
                    chunk,
                    [ExtractedEntity(name="Subject", type="concept")],
                    [
                        TimedFact(
                            subject="Subject",
                            predicate="related_to",
                            statement="candidate",
                        )
                    ],
                )

    dbutil.run(body())


def test_prepare_entities_passes_through_a_confident_type_unchanged(
    fake_embedder: RecordingEmbedder,
) -> None:
    entity = ExtractedEntity(name="Ada", type="author")

    async def body() -> str:
        async with User.system().session() as session:
            return (await prepare_entities(session, [entity]))[0].type

    assert dbutil.run(body()) == "author"


def test_prepare_entities_matches_a_declared_kind_for_a_concept_suggestion(
    fake_embedder: RecordingEmbedder,
) -> None:
    async def body() -> tuple[str, str]:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner).session() as session:
            async with session.begin():
                await Ontology.refresh(session)
                # Structural kinds are excluded from automatic suggestion matching.
                kind = (
                    await session.exec(
                        select(Entity.Kind).where(Entity.Kind.structural.is_(False)).limit(1)
                    )
                ).one()
                name, description = kind.name, kind.description
                kind.embedding = deterministic_vector(
                    f"document:{description}", settings.embed_dim
                )
                await session.flush()
            entity = ExtractedEntity(
                name="Something", type=System.Entity.CONCEPT, suggested_type=description
            )
            resolved = (await prepare_entities(session, [entity]))[0].type
        return resolved, name

    resolved_type, existing_name = dbutil.run(body())
    assert resolved_type == existing_name


def test_prepare_entities_keeps_a_novel_tenant_suggestion_as_concept(
    fake_embedder: RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ontology_match_threshold", 1.1)

    async def body() -> tuple[str, int, int]:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            before = await session.scalar(select(func.count()).select_from(Entity.Kind))
        async with dbutil.actor(owner).session() as session:
            resolved = (
                await prepare_entities(
                    session,
                    [
                        ExtractedEntity(
                            name="Something",
                            type=System.Entity.CONCEPT,
                            suggested_type=str(uuid5()),
                        )
                    ],
                )
            )[0].type
        async with dbutil.actor(owner) as session:
            after = await session.scalar(select(func.count()).select_from(Entity.Kind))
        return resolved, before or 0, after or 0

    resolved, before, after = dbutil.run(body())
    assert resolved == System.Entity.CONCEPT
    assert after == before


def test_prepare_entities_falls_back_to_concept_without_ontology_vectors(
    fake_embedder: RecordingEmbedder,
) -> None:
    entity = ExtractedEntity(
        name="Something", type=System.Entity.CONCEPT, suggested_type="a novel kind"
    )

    async def body() -> str:
        owner = await seedgraph.fresh_owner()
        user = dbutil.actor(owner)
        async with user.session() as session:
            async with session.begin():
                originals = list(
                    (
                        await session.exec(
                            select(Entity.Kind.name, Entity.Kind.embedding).where(
                                Entity.Kind.embedding.is_not(None)
                            )
                        )
                    ).all()
                )
                await session.exec(
                    update(Entity.Kind)
                    .values(embedding=None)
                    .execution_options(synchronize_session=False)
                )
            try:
                return (await prepare_entities(session, [entity]))[0].type
            finally:
                async with session.begin():
                    for name, vector in originals:
                        await session.exec(
                            update(Entity.Kind)
                            .where(Entity.Kind.name == name)
                            .values(embedding=vector)
                            .execution_options(synchronize_session=False)
                        )

    assert (
        dbutil.run(body()) == System.Entity.CONCEPT
    )  # no vectors to score means the concept stands


def test_build_graph_writes_a_slice_then_resumes(
    fake_llm: FakeLLM, fake_embedder: RecordingEmbedder, fake_gate: FakeGate
) -> None:
    fake_llm.register(
        WireExtraction,
        WireExtraction(
            e=[
                WireEntity(n="Ada", t="author"),
                WireEntity(n="Notes", t="concept"),
            ],
            f=[
                WireFact(
                    s="Ada",
                    p="uses",
                    o="Notes",
                    statement="Ada keeps notes",
                    quote=LONG_PROSE,
                )
            ],
        ),
    )

    async def body() -> tuple[tuple[int, int], tuple[int, int]]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE)
        first = await build_graph(scopes=frozenset({owner}))
        second = await build_graph(scopes=frozenset({owner}))
        return first, second

    first, second = dbutil.run(body())
    assert first == (2, 1)  # two entities minted, one binary fact between them
    assert second == (0, 0)  # the built chunk is skipped on resume


def test_build_graph_normalizes_unknown_types_and_drops_unknown_predicates(
    fake_llm: FakeLLM, fake_embedder: RecordingEmbedder, fake_gate: FakeGate
) -> None:
    fake_llm.register(
        WireExtraction,
        WireExtraction(
            e=[
                WireEntity(n="Ada", t="Author"),
                WireEntity(n="prompt rows", t="file"),
            ],
            f=[
                WireFact(
                    s="Ada",
                    p="Uses",
                    o="prompt rows",
                    statement="Ada reads prompt rows",
                    quote=LONG_PROSE,
                ),
                WireFact(
                    s="Ada",
                    p="references",
                    o="prompt rows",
                    statement="Ada references prompt rows",
                    quote=LONG_PROSE,
                ),
            ],
        ),
    )

    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE)
        return await build_graph(scopes=frozenset({owner}))

    assert dbutil.run(body()) == (2, 1)


def test_build_graph_source_filter_drops_a_path_and_skips_a_ghost_subject(
    fake_llm: FakeLLM, fake_embedder: RecordingEmbedder, fake_gate: FakeGate
) -> None:
    fake_llm.register(
        WireExtraction,
        WireExtraction(
            e=[
                WireEntity(n="Ada", t="author"),
                WireEntity(n="Ada", t="author"),
                WireEntity(n="notes/graph_rag.md", t="concept"),
            ],
            f=[
                WireFact(s="Ada", p="uses", statement="Ada keeps notes", quote=LONG_PROSE),
                WireFact(s="ghost", p="uses", statement="ghost drifts", quote=LONG_PROSE),
            ],
        ),
    )

    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE, title="alpha source")
        await seedgraph.seed_chunk(owner, LONG_PROSE, title="beta other")
        return await build_graph(scopes=frozenset({owner}), source="alpha")

    assert dbutil.run(body()) == (1, 1)  # Ada minted once, path dropped, ghost fact skipped


def test_pending_chunks_can_target_one_document() -> None:
    async def body() -> tuple[UUID7, list[Chunk]]:
        owner = await seedgraph.fresh_owner()
        first = await seedgraph.seed_chunk(owner, LONG_PROSE, title="first")
        await seedgraph.seed_chunk(owner, LONG_PROSE, title="second")
        async with dbutil.actor(owner) as session:
            chunk = await session.get(Chunk, first)
            assert chunk is not None
        selected = await build.pending_chunks(frozenset({owner}), None, None, chunk.document_id)
        return first, selected

    expected, selected = dbutil.run(body())
    assert [chunk.id for chunk in selected] == [expected]


def test_build_graph_skips_a_gated_out_chunk(fake_gate: FakeGate) -> None:
    fake_gate.result = False

    async def body() -> tuple[tuple[int, int], bool]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        result = await build_graph(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            done = await session.get(seedgraph.Chunk, chunk)
        return result, done is not None and done.processed_at is not None

    result, marked = dbutil.run(body())
    assert result == (0, 0)
    assert marked is True  # gated out, but still stamped done so it is never re-offered


def test_model_extraction_skips_the_separate_gate_for_a_self_gating_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity = ExtractedEntity(name="Ada", type="author")
    fact = TimedFact(
        subject="Ada",
        predicate="uses",
        statement="Ada writes",
        quote=LONG_PROSE,
    )

    class SelfGatingExtractor(build.Extractor):
        async def extract(self, text: str) -> Extraction:
            assert text == LONG_PROSE
            return Extraction(entities=[entity], facts=[fact])

    async def fail_gate(text: str) -> NoReturn:
        raise AssertionError(f"unexpected gate for {text}")

    monkeypatch.setattr(
        build.Extractor,
        "configured",
        classmethod(lambda cls: SelfGatingExtractor()),
    )
    monkeypatch.setattr(build, "relevant", fail_gate)
    owner = uuid5()
    chunk = Chunk(
        id=uuid5(),
        document_id=uuid5(),
        ord=0,
        text=LONG_PROSE,
        created_by=owner,
        scopes=[owner],
    )

    entities, facts = asyncio.run(build.model_extraction(chunk, None))

    assert entities == [entity]
    assert facts[0].valid_from is not None


@pytest.mark.parametrize(
    ("text", "title"),
    [("too short to bother", None), ("- 2024-01-01: an orphan dated line", None)],
    ids=["short-prose", "untitled-journal"],
)
def test_build_graph_marks_short_and_untitled_chunks_done(
    text: str, title: str | None, fake_embedder: RecordingEmbedder
) -> None:
    async def body() -> tuple[tuple[int, int], bool]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, text, title=title)
        result = await build_graph(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            done = await session.get(seedgraph.Chunk, chunk)
        return result, done is not None and done.processed_at is not None

    result, marked = dbutil.run(body())
    assert result == (0, 0)
    assert marked is True


def test_journal_line_logs_a_dated_project_fact(fake_embedder: RecordingEmbedder) -> None:
    journal_text = "# My Project\n\n- Type Project\n- 2024-01-01: shipped the first release"

    async def body() -> tuple[int, int, int]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, journal_text, title="My Project", subject_type="project")
        entities, facts = await build_graph(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            projects = (
                await session.exec(
                    select(func.count())
                    .select_from(Entity.Content)
                    .where(Entity.Content.type == "project")
                )
            ).one()
        return entities, facts, projects or 0

    entities, facts, projects = dbutil.run(body())
    assert entities >= 1 and facts >= 1
    assert projects == 1  # the title entity was lifted to a Project node


def test_short_typed_document_writes_its_declared_entity(
    fake_embedder: RecordingEmbedder,
) -> None:
    async def body() -> tuple[int, int, int]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(
            owner,
            "# Research\n\n- Type Area\n\nResearch is an area.",
            title="Research",
            subject_type="area",
        )
        entities, facts = await build_graph(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            areas = (
                await session.exec(
                    select(func.count())
                    .select_from(Entity.Content)
                    .where(Entity.Content.type == "area")
                )
            ).one()
        return entities, facts, areas or 0

    assert dbutil.run(body()) == (1, 0, 1)


def test_build_graph_propagates_an_extraction_timeout(
    fake_gate: FakeGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_raising_client(
        monkeypatch, APITimeoutError(request=httpx.Request("POST", "http://llm.invalid"))
    )

    async def body() -> None:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE)
        await build_graph(scopes=frozenset({owner}))

    with pytest.raises(APITimeoutError):
        dbutil.run(body())


def test_build_graph_raises_when_the_endpoint_is_unreachable(
    fake_gate: FakeGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_raising_client(
        monkeypatch, APIConnectionError(request=httpx.Request("POST", "http://llm.invalid"))
    )

    async def body() -> None:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE)
        await build_graph(scopes=frozenset({owner}))

    with pytest.raises(APIConnectionError):
        dbutil.run(body())


def test_build_graph_propagates_an_invalid_extraction(
    fake_gate: FakeGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValidationError) as caught:
        WireExtraction(e="truncated", f=[])
    install_raising_client(monkeypatch, caught.value)

    async def body() -> None:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE)
        await build_graph(scopes=frozenset({owner}))

    with pytest.raises(ValidationError):
        dbutil.run(body())


def test_build_graph_finishes_peers_then_raises_an_unexpected_chunk_error(
    fake_gate: FakeGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_raising_client(monkeypatch, RuntimeError("unexpected"))

    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE)
        return await build_graph(scopes=frozenset({owner}))

    with pytest.raises(RuntimeError, match="unexpected"):
        dbutil.run(body())


def test_build_graph_and_dedup_default_to_the_system_user_on_an_empty_graph() -> None:
    async def body() -> tuple[tuple[int, int], int]:
        await dbutil.reset_db()
        return await build_graph(), await dedup_entities()

    (entities, facts), merged = dbutil.run(body())
    assert (entities, facts) == (0, 0)
    assert merged == 0


def test_dedup_merges_a_slug_twin_and_converges(fake_embedder: RecordingEmbedder) -> None:
    canonical_id = entity_id("Team Memory", "concept")
    # Sort IDs so the fact-bearing duplicate is redirected into the canonical entity.
    duplicate_id = uuid.UUID(int=canonical_id.int + 1)

    async def body() -> tuple[int, int, int, bool]:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            await seedgraph.add_entity(session, owner, "Team Memory", content_id=canonical_id)
            await seedgraph.add_entity(session, owner, "team-memory", content_id=duplicate_id)
            fact, _ = await seedgraph.add_fact(
                session, owner, duplicate_id, statement="the duplicate carries a fact"
            )
        first = await dedup_entities(scopes=frozenset({owner}))
        second = await dedup_entities(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            survivors = list(await session.exec(select(Entity.Content.id)))
            subject = (
                await session.exec(select(Fact.Content.subject_id).where(Fact.Content.id == fact))
            ).one()
        return first, second, len(survivors), subject == survivors[0]

    first, second, survivors, repointed = dbutil.run(body())
    assert first == 1  # one duplicate merged away
    assert second == 0  # the rerun converges
    assert survivors == 1  # a single canonical node remains
    assert repointed is True  # the fact now names the survivor


@pytest.mark.parametrize("with_object", [False, True], ids=["subject-only", "with-object"])
def test_dedup_drops_a_path_like_entity_and_its_dangling_facts(
    with_object: bool, fake_embedder: RecordingEmbedder
) -> None:
    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            path_like = await seedgraph.add_entity(session, owner, "notes/graph_rag.md")
            object_id = (
                await seedgraph.add_entity(session, owner, "Ordinary Node")
                if with_object
                else None
            )
            await seedgraph.add_fact(
                session, owner, path_like, statement="a dangling fact", object_id=object_id
            )
        await dedup_entities(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            facts = (await session.exec(select(func.count()).select_from(Fact.Content))).one()
            entities = (await session.exec(select(func.count()).select_from(Entity.Content))).one()
        return facts or 0, entities or 0

    facts, entities = dbutil.run(body())
    assert facts == 0  # the dangling fact is dropped, never repointed to nothing
    assert entities == (1 if with_object else 0)  # only an ordinary object node survives


@pytest.mark.parametrize("subject_type", ["project", None], ids=["declared", "ordinary"])
def test_source_extraction_uses_the_document_type_across_chunks(
    subject_type: str | None,
) -> None:
    owner = uuid5()
    chunk = Chunk(
        id=uuid5(),
        document_id=uuid5(),
        ord=1,
        text="- 2024-01-01: shipped the first release",
        created_by=owner,
        scopes=[owner],
    )
    document = Document(
        content_hash=uuid8(),
        created_by=owner,
        scopes=[owner],
        title="My Project",
        subject_type=subject_type,
    )

    entities, facts = source_extraction(chunk, document)
    assert [entity.type for entity in entities] == [subject_type or System.Entity.CONCEPT]
    assert len(facts) == 1 and facts[0].subject == "My Project"


def test_source_declaration_becomes_generic_ontology_facts() -> None:
    owner = uuid5()
    observed = datetime(2026, 7, 15, tzinfo=UTC)
    chunk = Chunk(
        id=uuid5(),
        document_id=uuid5(),
        ord=0,
        text=(
            "# Aizk\n\n- Type Project\n- part_of [Area] Productivity\n- has_status [Status] Active"
        ),
        created_by=owner,
        scopes=[owner],
    )
    document = Document(
        content_hash=uuid8(),
        created_by=owner,
        scopes=[owner],
        title="Aizk",
        subject_type="project",
        observed_at=observed,
    )

    entities, facts = source_extraction(chunk, document)

    assert [(entity.name, entity.type) for entity in entities] == [
        ("Aizk", "project"),
        ("Productivity", "area"),
        ("Active", "status"),
    ]
    assert [(fact.predicate, fact.object_) for fact in facts] == [
        ("part_of", "Productivity"),
        ("has_status", "Active"),
    ]
    assert all(fact.valid_from == observed for fact in facts)


@pytest.mark.parametrize(
    ("relation", "expected"),
    [
        ("part_of [Area] Productivity", [("part_of", "Productivity")]),
        ("has_status [Status] Active", [("has_status", "Active")]),
    ],
)
def test_source_declaration_accepts_each_relation_independently(
    relation: str,
    expected: list[tuple[str, str]],
) -> None:
    owner = uuid5()
    chunk = Chunk(
        id=uuid5(),
        document_id=uuid5(),
        ord=0,
        text=f"# Aizk\n\n- Type Project\n- {relation}",
        created_by=owner,
        scopes=[owner],
    )
    document = Document(
        content_hash=uuid8(),
        created_by=owner,
        scopes=[owner],
        title="Aizk",
        subject_type="project",
    )

    _, facts = source_extraction(chunk, document)

    assert [(fact.predicate, fact.object_) for fact in facts] == expected


def test_graph_writer_skips_a_fact_whose_subject_did_not_resolve() -> None:
    owner = uuid5()

    class UnusedSession:
        pass

    writer = GraphWriter(cast("Session", UnusedSession()), owner, frozenset({owner}))
    fact = TimedFact(subject="Missing", predicate="uses", statement="Missing uses memory.")

    assert writer.candidate(fact, {}) is None


def test_raise_failures_groups_multiple_unexpected_chunk_errors() -> None:
    chunks = [
        Chunk(
            id=uuid5(),
            document_id=uuid5(),
            ord=index,
            text="x",
            created_by=uuid5(),
            scopes=[uuid5()],
        )
        for index in range(3)
    ]
    results: list[set[UUID5 | UUID7] | BaseException] = [set(), RuntimeError("a"), ValueError("b")]

    with pytest.raises(BaseExceptionGroup) as info:
        raise_failures(chunks, results)

    assert {type(error) for error in info.value.exceptions} == {RuntimeError, ValueError}


def test_forward_update_retires_the_prior_claim_and_opens_the_correction() -> None:
    base = datetime.now(UTC)
    later_start = base + timedelta(days=10)

    async def body() -> dict[str, tuple[bool, datetime | None, datetime | None]]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
            prior_object = await seedgraph.add_entity(session, owner, "Prior")
            newer_object = await seedgraph.add_entity(session, owner, "Newer")
            _, prior_claim = await seedgraph.add_fact(
                session,
                owner,
                subject,
                statement="the prior state",
                object_id=prior_object,
                embedding=E0,
                valid=Range(base, None),
            )
        fact = TimedFact(
            subject="Subject",
            predicate="related_to",
            object="Newer",
            statement="the newer state",
            valid_from=later_start,
        )
        async with dbutil.actor(owner) as session:
            candidate = FactCandidate(
                fact=fact,
                subject_id=subject,
                object_id=newer_object,
                identity=fact_id(subject, fact.predicate, newer_object, fact.statement),
            )
            await GraphWriter(session, owner, frozenset({owner})).apply_plans(
                [
                    FactPlan(
                        candidate=candidate,
                        vector=tuple(E0),
                        matches=(),
                        verdict=ConsolidationVerdict(action="UPDATE", supersedes=prior_claim),
                    )
                ],
                [],
                chunk,
            )
        async with dbutil.actor(owner) as session:
            rows = await session.exec(
                select(Fact.Content.statement, Fact.Claim.recorded, Fact.Claim.valid)
                .join(Fact.Claim, Fact.Claim.content_id == Fact.Content.id)
                .execution_options(**GATE_OFF)
            )
            return {
                statement: (recorded.upper_inf, valid.lower, valid.upper)
                for statement, recorded, valid in rows
            }

    claims = dbutil.run(body())
    assert claims["the prior state"] == (False, base, later_start)  # retired at the correction
    assert claims["the newer state"] == (True, later_start, None)  # the correction stays live


def test_update_without_event_time_closes_a_future_claim_as_empty() -> None:
    future = datetime.now(UTC) + timedelta(days=1)

    async def body() -> bool:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
            _, claim_id = await seedgraph.add_fact(
                session,
                owner,
                subject,
                statement="the scheduled state",
                embedding=E0,
                valid=Range(future, None),
            )
        async with dbutil.actor(owner) as session:
            fact = TimedFact(
                subject="Subject",
                predicate="related_to",
                statement="the replacement state",
            )
            await GraphWriter(session, owner, frozenset({owner})).apply_plans(
                [
                    FactPlan(
                        candidate=FactCandidate(
                            fact=fact,
                            subject_id=subject,
                            object_id=None,
                            identity=fact_id(
                                subject,
                                fact.predicate,
                                None,
                                fact.statement,
                            ),
                        ),
                        vector=tuple(E0),
                        matches=(),
                        verdict=ConsolidationVerdict(action="UPDATE", supersedes=claim_id),
                    )
                ],
                [],
                chunk,
            )
        async with dbutil.actor(owner) as session:
            claim = await session.get(Fact.Claim, claim_id, execution_options=GATE_OFF)
            assert claim is not None and claim.valid is not None
            return claim.valid.is_empty

    assert dbutil.run(body())


def test_write_graph_slice_retries_a_transient_db_conflict(
    fixed_embedder: FixedEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"n": 0}
    real_resolve = build.resolve_entities

    async def flaky(
        writer: GraphWriter, entities: list[PreparedEntity]
    ) -> dict[str, UUID5 | UUID7]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DBAPIError("stmt", None, WrappedDBAPI(TransactionRollbackError("deadlock")))
        return await real_resolve(writer, entities)

    monkeypatch.setattr(build, "resolve_entities", flaky)

    async def body() -> set[UUID5 | UUID7]:
        owner = await seedgraph.fresh_owner()
        chunk_id = await seedgraph.seed_chunk(owner, LONG_PROSE)
        chunk = Chunk(
            id=chunk_id,
            document_id=uuid5(),
            ord=0,
            text=LONG_PROSE,
            created_by=owner,
            scopes=[owner],
        )
        async with dbutil.actor(owner).session() as opened:
            return await write_graph_slice(
                opened,
                chunk,
                [ExtractedEntity(name="Ada", type="author")],
                [],
            )

    touched = dbutil.run(body())
    assert attempts["n"] == 2  # the first transient conflict retried into a clean write
    assert len(touched) == 1  # the retried attempt resolved and returned the Ada entity
