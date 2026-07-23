import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import NoReturn, cast

import dbutil
import httpx
import pytest
import seedgraph
from asyncpg.exceptions import TransactionRollbackError
from doubles import FakeLLM, RecordingEmbedder, deterministic_vector
from id_factory import uuid5, uuid7, uuid8
from openai import APIConnectionError, APITimeoutError
from pydantic import UUID5, UUID7, ValidationError
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import select

import aizk.graph.build as build
from aizk.config import settings
from aizk.extract.extractor import LLMExtractor
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
from aizk.graph.consolidation import Consolidator, FactMatch
from aizk.graph.ids import entity_id, fact_id
from aizk.graph.naming import normalize_name
from aizk.graph.repair import dedup_entities, redirect_entity
from aizk.graph.writer import FactCandidate, FactPlan, GraphWriter, PreparedEntity
from aizk.ontology import Ontology, System, WireEntity, WireExtraction, WireFact
from aizk.provenance import CaptureContext, EpistemicKind
from aizk.serving.embed import EmbedMode
from aizk.store import (
    Chunk,
    Document,
    Entity,
    Fact,
)
from aizk.store.engine import Session
from aizk.store.identity import User

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
    [vector] = await build.EmbedClient.from_settings(settings).embed([name], mode="document")
    return await writer.resolve(PreparedEntity(name=name, type=type, vector=tuple(vector)))


async def consolidate(
    writer: GraphWriter,
    facts: list[TimedFact],
    resolved: dict[str, UUID5 | UUID7],
    chunk: UUID5 | UUID7,
) -> None:
    candidates = await writer.new_candidates(
        facts, {normalize_name(name): entity for name, entity in resolved.items()}
    )
    vectors = await build.EmbedClient.from_settings(settings).embed(
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
    monkeypatch.setattr(build.GateClient, "from_settings", classmethod(lambda cls, config: gate))
    return gate


@pytest.fixture
def fixed_embedder(monkeypatch: pytest.MonkeyPatch) -> FixedEmbedder:
    embedder = FixedEmbedder(E0)
    monkeypatch.setattr(
        build.EmbedClient, "from_settings", classmethod(lambda cls, config: embedder)
    )
    return embedder


def graph_writer(
    session: Session,
    created_by: UUID5 | UUID7,
    scopes: frozenset[UUID5 | UUID7],
    llm: FakeLLM | None = None,
    capture: CaptureContext | None = None,
    source_text: str = "",
) -> GraphWriter:
    """A writer over a fake consolidation model, the test-side composition root."""
    return GraphWriter(
        session=session,
        created_by=created_by,
        scopes=scopes,
        consolidator=Consolidator(llm=(llm or FakeLLM()).llm),
        capture=capture or CaptureContext(),
        source_text=source_text,
    )


def clients_for(
    llm: FakeLLM | None = None,
    embed: RecordingEmbedder | None = None,
    gate: FakeGate | None = None,
) -> build.GraphClients:
    """A graph client bundle over fakes, mirroring what the runtime threads in."""
    model = (llm or FakeLLM()).llm
    return build.GraphClients(
        extractor=LLMExtractor(llm=model),
        gate=gate or FakeGate(),
        embed=embed or RecordingEmbedder(),
        llm=model,
    )


def raising_llm(error: BaseException) -> FakeLLM:
    llm = FakeLLM()
    llm.completions.error = error
    return llm


def test_build_graph_loads_ontology_for_a_fresh_process(
    monkeypatch: pytest.MonkeyPatch, fixed_embedder: FixedEmbedder
) -> None:
    monkeypatch.setattr(Ontology, "_cached", None)

    assert dbutil.run(build_graph(clients_for(embed=fixed_embedder), limit=0)) == (0, 0)
    assert Ontology.current().entity_names

    async def invalid_mint() -> None:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            with pytest.raises(IntegrityError):
                await Entity.Content.mint_all(
                    session,
                    [Entity.Content(id=uuid5(), name="invalid", type="missing ontology kind")],
                )

    dbutil.run(invalid_mint())


class WrappedDBAPI(Exception):
    def __init__(self, inner: BaseException) -> None:
        self.orig = inner


def test_build_helpers_classify_errors_redirect_ids_and_group_failures() -> None:
    errors = [
        (DBAPIError("s", None, WrappedDBAPI(TransactionRollbackError("deadlock"))), True),
        (DBAPIError("s", None, WrappedDBAPI(ValueError("other"))), False),
        (ValueError("not a db error"), False),
    ]
    assert [is_transient_db_error(error) for error, _ in errors] == [
        expected for _, expected in errors
    ]
    canonical, duplicate, dropped, untouched = (uuid5() for _ in range(4))
    redirect = {duplicate: canonical, dropped: None}
    assert redirect_entity(redirect, None) == (None, False)
    assert redirect_entity(redirect, untouched) == (untouched, False)
    assert redirect_entity(redirect, duplicate) == (canonical, False)
    assert redirect_entity(redirect, dropped) == (None, True)
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
    results: list[set[UUID5 | UUID7] | BaseException] = [
        set(),
        RuntimeError("a"),
        ValueError("b"),
    ]
    with pytest.raises(BaseExceptionGroup) as info:
        raise_failures(chunks, results)
    assert {type(error) for error in info.value.exceptions} == {RuntimeError, ValueError}


@pytest.mark.parametrize(
    "scenario",
    [
        "insert",
        "exact",
        "path",
        "semantic",
        "distinct-type",
        "subject-missing",
        "object-missing",
        "both-resolved",
    ],
)
def test_resolve_uses_normalized_names_and_types_as_exact_entity_identity(
    scenario: str, fake_embedder: RecordingEmbedder
) -> None:
    if scenario.endswith("missing") or scenario == "both-resolved":
        owner, subject, object_id = uuid5(), uuid5(), uuid5()
        writer = graph_writer(Session(), owner, frozenset({owner}))
        if scenario == "subject-missing":
            fact = TimedFact(subject="Missing", predicate="uses", statement="Missing uses memory.")
            assert writer.candidate(fact, {}) is None
        elif scenario == "object-missing":
            fact = TimedFact(
                subject="Known",
                predicate="uses",
                object="Missing",
                statement="Known uses Missing.",
            )
            assert writer.candidate(fact, {"known": subject}) is None
        else:
            fact = TimedFact(
                subject="MoE Expert Compression",
                predicate="uses",
                object="RTX 3090",
                statement="MoE expert compression uses the RTX 3090.",
            )
            candidate = writer.candidate(
                fact, {"moe expert compression": subject, "rtx 3090": object_id}
            )
            assert candidate is not None
            assert (candidate.subject_id, candidate.object_id) == (subject, object_id)
        return

    async def body() -> tuple[UUID5 | UUID7 | None, ...]:
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
            if scenario == "distinct-type":
                await seedgraph.add_entity(
                    session,
                    owner,
                    "Research",
                    type="area",
                    content_id=entity_id("Research", "area"),
                )
        async with dbutil.actor(owner) as session:
            writer = graph_writer(session, owner, frozenset({owner}))
            if scenario == "insert":
                first = await resolve(writer, "Brand New", "concept")
                second = await resolve(writer, "Brand New", "concept")
                return first, second
            if scenario == "exact":
                return (await resolve(writer, "Exact Fixture", "author"),)
            if scenario == "path":
                return (await resolve(writer, "notes/graph_rag.md", "concept"),)
            if scenario == "semantic":
                return (
                    await resolve(writer, "Research", "area"),
                    await resolve(writer, "Business", "area"),
                )
            return (
                entity_id("Research", "area"),
                await resolve(writer, "research", System.Entity.CONCEPT),
            )

    expected = {
        "insert": (entity_id("Brand New", "concept"),) * 2,
        "exact": (entity_id("Exact Fixture", "author"),),
        "path": (None,),
        "semantic": (entity_id("Research", "area"), entity_id("Business", "area")),
        "distinct-type": (
            entity_id("Research", "area"),
            entity_id("research", System.Entity.CONCEPT),
        ),
    }
    assert dbutil.run(body()) == expected[scenario]


def deterministic(text: str) -> list[float]:
    return deterministic_vector(text, settings.embed_dim)


@pytest.mark.parametrize("scenario", ["add", "noop", "update", "duplicate", "idempotent"])
def test_consolidate_applies_verdict(scenario: str, fixed_embedder: FixedEmbedder) -> None:
    if scenario == "duplicate":
        check_duplicate_state_candidate(fixed_embedder)
        return
    if scenario == "idempotent":
        check_idempotent_consolidation(fixed_embedder)
        return
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
                graph_writer(session, owner, frozenset({owner})),
                [fact],
                resolved,
                chunk,
            )
        async with dbutil.actor(owner) as session:
            total = (
                await session.exec(select(Fact.Claim.id.count()).execution_options(**GATE_OFF))
            ).one()
            live = (
                await session.exec(
                    select(Fact.Live.id.count()).where(Fact.Live.subject_id == subject)
                )
            ).one()
        return total or 0, live or 0

    assert dbutil.run(body()) == {"add": (1, 1), "noop": (1, 1), "update": (2, 1)}[scenario]


def check_duplicate_state_candidate(fixed_embedder: FixedEmbedder) -> None:
    now = datetime.now(UTC)

    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Productivity")
            archived = await seedgraph.add_entity(session, owner, "Archived")
            maintained = await seedgraph.add_entity(session, owner, "Maintained")
            await seedgraph.add_fact(
                session,
                owner,
                subject,
                statement="Status Archived",
                predicate="has_status",
                object_id=archived,
                embedding=E0,
                valid=Range(now - timedelta(days=2), None),
            )
        fact = TimedFact(
            subject="Productivity",
            predicate="has_status",
            object="Maintained",
            statement="Productivity has status Maintained.",
            valid_from=now - timedelta(days=1),
        )
        model_fact = fact.model_copy(update={"statement": "has_status [Status] Maintained"})
        async with dbutil.actor(owner) as session:
            await consolidate(
                graph_writer(session, owner, frozenset({owner})),
                [fact, model_fact],
                {"Productivity": subject, "Maintained": maintained},
                chunk,
            )
        async with dbutil.actor(owner) as session:
            total = (
                await session.exec(select(Fact.Claim.id.count()).execution_options(**GATE_OFF))
            ).one()
            live = (await session.exec(select(Fact.Live.id.count()))).one()
        return total or 0, live or 0

    assert dbutil.run(body()) == (2, 1)


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
            writer = graph_writer(session, owner, frozenset({owner}), source_text=LONG_PROSE)
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


def check_idempotent_consolidation(fixed_embedder: FixedEmbedder) -> None:
    async def body() -> tuple[list[FactPlan], int]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
        fact = TimedFact(subject="Subject", predicate="uses", statement="only")
        async with dbutil.actor(owner) as session:
            writer = graph_writer(session, owner, frozenset({owner}))
            empty = await writer.plan_facts([], [])
            await consolidate(writer, [fact], {"Subject": subject}, chunk)
        async with dbutil.actor(owner) as session:
            await consolidate(
                graph_writer(session, owner, frozenset({owner})),
                [fact],
                {"Subject": subject},
                chunk,
            )
        async with dbutil.actor(owner) as session:
            total = (
                await session.exec(select(Fact.Claim.id.count()).execution_options(**GATE_OFF))
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
                    clients_for(embed=fixed_embedder),
                )

        await asyncio.gather(
            *(write(chunk_id, fact) for chunk_id, fact in zip(chunk_ids, facts, strict=True))
        )
        async with dbutil.actor(owner) as session:
            return (await session.exec(select(Fact.Live.id.count()))).one()

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
                writer = graph_writer(
                    session,
                    speaker,
                    frozenset({scope}),
                    capture=CaptureContext(speaker_label=label, speaker_role="Analyst"),
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
                    ConsolidationVerdict(action="NOOP"),
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
                graph_writer(session, owner, frozenset({owner}), llm=fake_llm),
                facts,
                {"Subject": subject},
                chunk,
            )
        async with dbutil.actor(owner) as session:
            return (
                await session.exec(select(Fact.Claim.id.count()).execution_options(**GATE_OFF))
            ).one()

    assert dbutil.run(body()) == 2
    assert len(fake_llm.completions.calls) == 1
    assert fake_llm.completions.calls[0].response_model is BatchConsolidationVerdict


@pytest.mark.parametrize(
    "scenario", ["backdated-open", "backdated-bounded", "forward", "future-empty"]
)
def test_temporal_updates_preserve_valid_and_recorded_history(scenario: str) -> None:
    if scenario.startswith("backdated"):
        check_backdated_update(scenario == "backdated-bounded")
    elif scenario == "forward":
        check_forward_update()
    else:
        check_future_claim_closed_as_empty()


def check_backdated_update(bounded: bool) -> None:
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
            await graph_writer(session, owner, frozenset({owner})).apply_plans(
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
                select(
                    Fact.Content.statement,
                    Fact.Claim.recorded_to,
                    Fact.Claim.valid_from,
                    Fact.Claim.valid_to,
                )
                .join(Fact.Claim, Fact.Claim.content_id == Fact.Content.id)
                .execution_options(**GATE_OFF)
            )
            return {
                statement: (recorded_to is None, valid_from, valid_to)
                for statement, recorded_to, valid_from, valid_to in rows
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
                    clients_for(embed=fixed_embedder),
                )

    dbutil.run(body())


@pytest.mark.parametrize("scenario", ["declared", "matched", "novel", "unembedded"])
def test_prepare_entities_resolves_types_without_mutating_the_ontology(
    scenario: str,
    fake_embedder: RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def body() -> tuple[str, str, tuple[int, int] | None]:
        if scenario == "declared":
            async with User.system().session() as session:
                resolved = await prepare_entities(
                    session, [ExtractedEntity(name="Ada", type="author")], fake_embedder
                )
            return resolved[0].type, "author", None

        owner = await seedgraph.fresh_owner()
        if scenario == "novel":
            monkeypatch.setattr(settings, "ontology_match_threshold", 1.1)
            async with dbutil.actor(owner) as session:
                before = await session.scalar(select(Entity.Kind.name.count()))
            async with dbutil.actor(owner).session() as session:
                resolved = await prepare_entities(
                    session,
                    [
                        ExtractedEntity(
                            name="Something",
                            type=System.Entity.CONCEPT,
                            suggested_type=str(uuid5()),
                        )
                    ],
                    fake_embedder,
                )
            async with dbutil.actor(owner) as session:
                after = await session.scalar(select(Entity.Kind.name.count()))
            return resolved[0].type, System.Entity.CONCEPT, (before or 0, after or 0)

        async with dbutil.actor(owner).session() as session:
            async with session.begin():
                await Ontology.refresh(session)
                originals = (
                    list(
                        (
                            await session.exec(
                                select(Entity.Kind.name, Entity.Kind.embedding).where(
                                    Entity.Kind.embedding.is_not(None)
                                )
                            )
                        ).all()
                    )
                    if scenario == "unembedded"
                    else []
                )
                if scenario == "unembedded":
                    await session.exec(
                        update(Entity.Kind)
                        .values(embedding=None)
                        .execution_options(synchronize_session=False)
                    )
            if scenario == "unembedded":
                entity = ExtractedEntity(
                    name="Something",
                    type=System.Entity.CONCEPT,
                    suggested_type="a novel kind",
                )
                try:
                    resolved = await prepare_entities(session, [entity], fake_embedder)
                finally:
                    async with session.begin():
                        for name, vector in originals:
                            await session.exec(
                                update(Entity.Kind)
                                .where(Entity.Kind.name == name)
                                .values(embedding=vector)
                                .execution_options(synchronize_session=False)
                            )
                return resolved[0].type, System.Entity.CONCEPT, None
            async with session.begin():
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
            resolved = (await prepare_entities(session, [entity], fake_embedder))[0].type
        return resolved, name, None

    resolved, expected, counts = dbutil.run(body())
    assert resolved == expected
    assert counts is None or counts[0] == counts[1]


@pytest.mark.parametrize("scenario", ["ordinary", "normalized", "filtered"])
def test_build_graph_normalizes_filters_and_resumes_without_duplicate_work(
    scenario: str,
    fake_llm: FakeLLM,
    fake_embedder: RecordingEmbedder,
    fake_gate: FakeGate,
) -> None:
    if scenario == "ordinary":
        extraction = WireExtraction(
            e=[WireEntity(n="Ada", t="author"), WireEntity(n="Notes", t="concept")],
            f=[
                WireFact(
                    s="Ada",
                    p="uses",
                    o="Notes",
                    statement="Ada keeps notes",
                    quote=LONG_PROSE,
                )
            ],
        )
    elif scenario == "normalized":
        extraction = WireExtraction(
            e=[WireEntity(n="Ada", t="Author"), WireEntity(n="prompt rows", t="file")],
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
        )
    else:
        extraction = WireExtraction(
            e=[
                WireEntity(n="Ada", t="author"),
                WireEntity(n="Ada", t="author"),
                WireEntity(n="notes/graph_rag.md", t="concept"),
            ],
            f=[
                WireFact(s="Ada", p="uses", statement="Ada keeps notes", quote=LONG_PROSE),
                WireFact(s="ghost", p="uses", statement="ghost drifts", quote=LONG_PROSE),
            ],
        )
    fake_llm.register(
        WireExtraction,
        extraction,
    )

    async def body() -> tuple[tuple[int, int], tuple[int, int]]:
        owner = await seedgraph.fresh_owner()
        first_chunk = await seedgraph.seed_chunk(
            owner, LONG_PROSE, title="alpha source" if scenario == "filtered" else None
        )
        if scenario == "filtered":
            await seedgraph.seed_chunk(owner, LONG_PROSE, title="beta other")
            async with dbutil.actor(owner) as session:
                chunk = await session.get(Chunk, first_chunk)
                assert chunk is not None
            selected = await build.pending_chunks(
                frozenset({owner}), None, None, chunk.document_id
            )
            assert [pending.id for pending in selected] == [first_chunk]
        clients = clients_for(fake_llm, fake_embedder, fake_gate)
        source = "alpha" if scenario == "filtered" else None
        first = await build_graph(clients, scopes=frozenset({owner}), source=source)
        second = await build_graph(clients, scopes=frozenset({owner}), source=source)
        return first, second

    first, second = dbutil.run(body())
    assert first == ((1, 1) if scenario == "filtered" else (2, 1))
    assert second == (0, 0)


@pytest.mark.parametrize(
    "scenario", ["gated", "self-gating", "short", "untitled-journal", "journal", "typed"]
)
def test_build_graph_handles_gates_and_source_only_extraction_paths(
    scenario: str,
    fake_gate: FakeGate,
    fake_embedder: RecordingEmbedder,
) -> None:
    if scenario == "self-gating":
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

        clients = build.GraphClients(
            extractor=SelfGatingExtractor(),
            gate=cast("build.RelevanceGate", SimpleNamespace(relevant=fail_gate)),
            embed=RecordingEmbedder(),
            llm=FakeLLM().llm,
        )
        owner = uuid5()
        chunk = Chunk(
            id=uuid5(),
            document_id=uuid5(),
            ord=0,
            text=LONG_PROSE,
            created_by=owner,
            scopes=[owner],
        )
        entities, facts = asyncio.run(build.model_extraction(chunk, None, clients))
        assert entities == [entity]
        assert facts[0].valid_from is not None
        return

    async def body() -> None:
        owner = await seedgraph.fresh_owner()
        if scenario in {"gated", "short", "untitled-journal"}:
            fake_gate.result = scenario != "gated"
            text = {
                "gated": LONG_PROSE,
                "short": "too short to bother",
                "untitled-journal": "- 2024-01-01: an orphan dated line",
            }[scenario]
            chunk = await seedgraph.seed_chunk(owner, text)
            result = await build_graph(
                clients_for(embed=fake_embedder, gate=fake_gate),
                scopes=frozenset({owner}),
            )
            async with dbutil.actor(owner) as session:
                done = await session.get(seedgraph.Chunk, chunk)
            assert result == (0, 0)
            assert done is not None and done.processed_at is not None
            return

        title, subject_type, text, expected = (
            (
                "My Project",
                "project",
                "# My Project\n\n- Type Project\n- 2024-01-01: shipped the first release",
                ("project", True),
            )
            if scenario == "journal"
            else (
                "Research",
                "area",
                "# Research\n\n- Type Area\n\nResearch is an area.",
                ("area", False),
            )
        )
        await seedgraph.seed_chunk(owner, text, title=title, subject_type=subject_type)
        entities, facts = await build_graph(
            clients_for(embed=fake_embedder), scopes=frozenset({owner})
        )
        async with dbutil.actor(owner) as session:
            typed = (
                await session.exec(
                    select(Entity.Content.id.count()).where(Entity.Content.type == expected[0])
                )
            ).one()
        assert typed == 1
        assert entities >= 1
        assert bool(facts) is expected[1]

    dbutil.run(body())


def raising_error(kind: str) -> BaseException:
    """Build one extraction-time failure that must propagate out of the single-chunk build."""
    request = httpx.Request("POST", "http://llm.invalid")
    if kind == "timeout":
        return APITimeoutError(request=request)
    if kind == "connection":
        return APIConnectionError(request=request)
    if kind == "runtime":
        return RuntimeError("unexpected")
    try:
        WireExtraction(e="truncated", f=[])
    except ValidationError as invalid:
        return invalid
    raise AssertionError("a truncated WireExtraction payload must not validate")


@pytest.mark.parametrize("kind", ["timeout", "connection", "validation", "runtime"])
def test_build_graph_propagates_a_single_chunk_extraction_failure(
    kind: str, fake_gate: FakeGate
) -> None:
    error = raising_error(kind)
    clients = clients_for(raising_llm(error), gate=fake_gate)

    async def body() -> None:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE)
        await build_graph(clients, scopes=frozenset({owner}))

    with pytest.raises(type(error)):
        dbutil.run(body())


@pytest.mark.parametrize("scenario", ["empty", "slug", "path", "path-with-object"])
def test_dedup_repairs_normalized_duplicates_and_dangling_paths(
    scenario: str, fake_embedder: RecordingEmbedder
) -> None:
    async def body() -> None:
        if scenario == "empty":
            await dbutil.reset_db()
            assert await build_graph(clients_for()) == (0, 0)
            assert await dedup_entities() == 0
            return

        owner = await seedgraph.fresh_owner()
        if scenario.startswith("path"):
            async with dbutil.actor(owner) as session:
                path_like = await seedgraph.add_entity(session, owner, "notes/graph_rag.md")
                object_id = (
                    await seedgraph.add_entity(session, owner, "Ordinary Node")
                    if scenario == "path-with-object"
                    else None
                )
                await seedgraph.add_fact(
                    session, owner, path_like, statement="a dangling fact", object_id=object_id
                )
            await dedup_entities(scopes=frozenset({owner}))
            async with dbutil.actor(owner) as session:
                facts = (await session.exec(select(Fact.Content.id.count()))).one()
                entities = (await session.exec(select(Entity.Content.id.count()))).one()
            assert facts == 0
            assert entities == (1 if object_id is not None else 0)
            return

        canonical_id = entity_id("Team Memory", "concept")
        duplicate_id = uuid.UUID(int=canonical_id.int + 1)
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
        assert (first, second, len(survivors), subject == survivors[0]) == (1, 0, 1, True)

    dbutil.run(body())


@pytest.mark.parametrize(
    "scenario",
    ["journal-project", "journal-concept", "declaration", "tags", "part-of", "status"],
)
def test_source_extraction_projects_journals_declarations_and_tags(scenario: str) -> None:
    owner = uuid5()
    observed = datetime(2026, 7, 15, tzinfo=UTC)
    if scenario.startswith("journal"):
        subject_type = "project" if scenario == "journal-project" else None
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
        return

    relation = {
        "part-of": "part_of [Area] Productivity",
        "status": "has_status [Status] Active",
    }.get(scenario)
    text = {
        "declaration": (
            "# Aizk\n\n- Type Project\n- part_of [Area] Productivity\n- has_status [Status] Active"
        ),
        "tags": "# Ontology boundary\n\n#project: AIZK Productization\n#area: Business",
    }.get(scenario, f"# Aizk\n\n- Type Project\n- {relation}")
    title = "Ontology boundary" if scenario == "tags" else "Aizk"
    chunk = Chunk(
        id=uuid5(),
        document_id=uuid5(),
        ord=0,
        text=text,
        created_by=owner,
        scopes=[owner],
    )
    document = Document(
        content_hash=uuid8(),
        created_by=owner,
        scopes=[owner],
        title=title,
        subject_type=None if scenario == "tags" else "project",
        observed_at=observed,
    )

    entities, facts = source_extraction(chunk, document)
    if scenario == "tags":
        assert [(entity.name, entity.type) for entity in entities] == [
            ("Ontology boundary", System.Entity.CONCEPT),
            ("AIZK Productization", "project"),
            ("Business", "area"),
        ]
        assert [(fact.predicate, fact.object_) for fact in facts] == [
            (System.Relation.RELATED_TO, "AIZK Productization"),
            (System.Relation.RELATED_TO, "Business"),
        ]
        return
    expected = {
        "declaration": [("part_of", "Productivity"), ("has_status", "Active")],
        "part-of": [("part_of", "Productivity")],
        "status": [("has_status", "Active")],
    }[scenario]
    assert [(fact.predicate, fact.object_) for fact in facts] == expected
    assert all(fact.valid_from == observed for fact in facts)
    if scenario == "declaration":
        assert [(entity.name, entity.type) for entity in entities] == [
            ("Aizk", "project"),
            ("Productivity", "area"),
            ("Active", "status"),
        ]


def check_forward_update() -> None:
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
            await graph_writer(session, owner, frozenset({owner})).apply_plans(
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
                select(
                    Fact.Content.statement,
                    Fact.Claim.recorded_to,
                    Fact.Claim.valid_from,
                    Fact.Claim.valid_to,
                )
                .join(Fact.Claim, Fact.Claim.content_id == Fact.Content.id)
                .execution_options(**GATE_OFF)
            )
            return {
                statement: (recorded_to is None, valid_from, valid_to)
                for statement, recorded_to, valid_from, valid_to in rows
            }

    claims = dbutil.run(body())
    assert claims["the prior state"] == (False, base, later_start)  # retired at the correction
    assert claims["the newer state"] == (True, later_start, None)  # the correction stays live


def check_future_claim_closed_as_empty() -> None:
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
            await graph_writer(session, owner, frozenset({owner})).apply_plans(
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
            assert claim is not None
            return claim.valid_from == claim.valid_to

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
                clients_for(embed=fixed_embedder),
            )

    touched = dbutil.run(body())
    assert attempts["n"] == 2  # the first transient conflict retried into a clean write
    assert len(touched) == 1  # the retried attempt resolved and returned the Ada entity
