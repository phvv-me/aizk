import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, NoReturn, cast

import dbutil
import httpx
import pytest
import seedgraph
from asyncpg.exceptions import TransactionRollbackError
from doubles import FakeLLM, RecordingEmbedder, deterministic_vector
from openai import APIConnectionError, APITimeoutError, LengthFinishReasonError
from pydantic import BaseModel, ValidationError
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import select

import aizk.graph.build as build
from aizk.config import settings
from aizk.exceptions import ExtractionUnreachableError
from aizk.extract import ontology
from aizk.extract.llm import decide_consolidations_batch
from aizk.extract.llm import triples as llm_triples
from aizk.extract.models import (
    BatchConsolidationVerdict,
    ConsolidationVerdict,
    ExtractedEntity,
    TimedFact,
)
from aizk.extract.ontology import cache as ontology_cache
from aizk.graph.build import (
    build_graph,
    is_transient_db_error,
    journal_extraction,
    prepare_entities,
    raise_failures,
    write_graph_slice,
)
from aizk.graph.consolidation import FactMatch
from aizk.graph.ids import entity_id, fact_id
from aizk.graph.repair import dedup_entities, redirect_entity
from aizk.graph.writer import FactCandidate, FactPlan, GraphWriter, PreparedEntity
from aizk.provenance import CaptureContext, EpistemicKind
from aizk.serving.embed import EmbedMode
from aizk.store import (
    Chunk,
    Document,
    EntityContent,
    EntityKind,
    FactClaim,
    FactContent,
    LiveFact,
    session_for,
)

if TYPE_CHECKING:
    from aizk.store.engine import Session

pytestmark = pytest.mark.usefixtures("migrated_db")

# Long enough to enter the LLM extraction path
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


async def resolve(writer: GraphWriter, name: str, type: str) -> uuid.UUID | None:
    [vector] = await build.embed([name], mode="document")
    return await writer.resolve(PreparedEntity(name=name, type=type, vector=tuple(vector)))


async def consolidate(
    writer: GraphWriter,
    facts: list[TimedFact],
    resolved: dict[str, uuid.UUID],
    chunk: uuid.UUID,
) -> None:
    candidates = await writer.new_candidates(facts, resolved)
    vectors = await build.embed(
        [candidate.fact.statement for candidate in candidates], mode="document"
    )
    plans = await writer.plan_facts(candidates, vectors)
    borderline = writer.borderline(plans)
    decisions = await decide_consolidations_batch(borderline) if borderline else []
    await writer.apply_plans(plans, decisions, chunk)


class FakeGate:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[str] = []

    async def relevant(self, text: str) -> bool:
        self.calls.append(text)
        return self.result


class RaisingCompletions:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def parse(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: type[BaseModel],
        temperature: float | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, list[str]] | None = None,
    ) -> NoReturn:
        raise self.error


class RaisingClient:
    def __init__(self, error: BaseException) -> None:
        self.chat = type("Chat", (), {"completions": RaisingCompletions(error)})()


@pytest.fixture
def fake_gate(monkeypatch: pytest.MonkeyPatch) -> FakeGate:
    gate = FakeGate()
    monkeypatch.setattr(build, "relevant", gate.relevant)
    return gate


@pytest.fixture
def fixed_embedder(monkeypatch: pytest.MonkeyPatch) -> FixedEmbedder:
    embedder = FixedEmbedder(E0)
    monkeypatch.setattr(build, "embed", embedder.embed)
    monkeypatch.setattr(ontology_cache, "embed", embedder.embed)
    return embedder


def install_raising_client(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    client = RaisingClient(error)
    monkeypatch.setattr(llm_triples, "client_for", lambda *args, **kwargs: client)


def test_build_graph_loads_ontology_for_a_fresh_process(
    monkeypatch: pytest.MonkeyPatch, fixed_embedder: FixedEmbedder
) -> None:
    monkeypatch.setattr(ontology_cache, "_snapshot", None)

    assert dbutil.run(build_graph(limit=0)) == (0, 0)
    assert ontology.current().entity_names


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
    canonical, duplicate, dropped, untouched = (uuid.uuid4() for _ in range(4))
    redirect = {duplicate: canonical, dropped: None}
    assert redirect_entity(redirect, None) == (None, False)
    assert redirect_entity(redirect, untouched) == (untouched, False)
    assert redirect_entity(redirect, duplicate) == (canonical, False)
    assert redirect_entity(redirect, dropped) == (None, True)


@pytest.mark.parametrize("scenario", ["insert", "exact", "fuzzy", "path"])
def test_resolve_mints_reuses_folds_or_drops(
    scenario: str, fake_embedder: RecordingEmbedder
) -> None:
    async def body() -> uuid.UUID | None:
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


def test_batch_mint_reraises_a_non_unique_integrity_error() -> None:
    async def body() -> None:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            with pytest.raises(IntegrityError):
                await EntityContent.mint_all(
                    session, [EntityContent(name="invalid", type="missing ontology kind")]
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
                    object_id=target,
                    embedding=E0,
                    valid=Range(now, None) if scenario == "update" else None,
                )
            if scenario == "update":
                resolved["Obj Two"] = await seedgraph.add_entity(session, owner, "Obj Two")
        fact = TimedFact(
            subject="Subject",
            predicate="related_to",
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
                    select(func.count()).select_from(FactClaim).execution_options(**GATE_OFF)
                )
            ).one()
            live = (
                await session.exec(
                    select(func.count())
                    .select_from(LiveFact)
                    .where(LiveFact.subject_id == subject)
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
        fact = TimedFact(
            subject="Subject", predicate="related_to", statement="grounded", quote=quote
        )
        async with dbutil.actor(owner) as session:
            writer = GraphWriter(session, owner, frozenset({owner}), source_text=LONG_PROSE)
            await consolidate(writer, [fact], {"Subject": subject}, chunk)
        async with dbutil.actor(owner) as session:
            return (
                await session.exec(
                    select(FactClaim.attributes)
                    .where(FactClaim.source_chunk_id == chunk)
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
        fact = TimedFact(subject="Subject", predicate="related_to", statement="only")
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
                    select(func.count()).select_from(FactClaim).execution_options(**GATE_OFF)
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
                predicate="related_to",
                statement=f"Subject carries concurrent state {index}",
            )
            for index in range(2)
        ]

        async def write(chunk_id: uuid.UUID, fact: TimedFact) -> None:
            async with session_for(dbutil.actor(owner)) as opened:
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
            return (await session.exec(select(func.count()).select_from(LiveFact))).one()

    assert dbutil.run(body()) == 1


def test_speaker_bound_claims_coexist_inside_one_shared_scope(
    fixed_embedder: FixedEmbedder,
) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()

    async def body() -> list[tuple[str, str | None]]:
        scope = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(scope, LONG_PROSE)
        async with dbutil.actor(scope) as session:
            subject = await seedgraph.add_entity(session, scope, "Subject")
        opinion = TimedFact(
            subject="Subject",
            predicate="related_to",
            statement="The plan looks risky.",
            kind=EpistemicKind.opinion,
        )
        for speaker, label in ((alice, "Alice"), (bob, "Bob")):
            async with dbutil.actor(scope) as session:
                writer = GraphWriter(
                    session,
                    speaker,
                    frozenset({scope}),
                    CaptureContext(speaker_label=label, speaker_role="Reviewer"),
                )
                await consolidate(writer, [opinion], {"Subject": subject}, chunk)
        async with dbutil.actor(scope) as session:
            rows = await session.exec(
                select(LiveFact.perspective_key, LiveFact.attributes).where(
                    LiveFact.subject_id == subject
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
    phantom = uuid.uuid4()

    async def body() -> int:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
            await seedgraph.add_fact(session, owner, subject, statement="seeded", embedding=E_BAND)
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
            TimedFact(subject="Subject", predicate="related_to", statement=text)
            for text in ("first candidate", "second candidate")
        ]
        async with dbutil.actor(owner) as session:
            await consolidate(
                GraphWriter(session, owner, frozenset({owner})), facts, {"Subject": subject}, chunk
            )
        async with dbutil.actor(owner) as session:
            return (
                await session.exec(
                    select(func.count()).select_from(FactClaim).execution_options(**GATE_OFF)
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
                identity=fact_id(fact.subject, fact.predicate, fact.object_, fact.statement),
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
                select(FactContent.statement, FactClaim.recorded, FactClaim.valid)
                .join(FactClaim, FactClaim.content_id == FactContent.id)
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
                        id=uuid.uuid4(),
                        predicate="related_to",
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
        async with session_for(dbutil.actor(owner)) as opened:
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
        return (await prepare_entities([entity]))[0].type

    assert dbutil.run(body()) == "author"


def test_prepare_entities_matches_a_curated_kind_for_a_concept_suggestion(
    fake_embedder: RecordingEmbedder,
) -> None:
    async def body() -> tuple[str, str]:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            await ontology.refresh(session)
            # Structural kinds are excluded from automatic suggestion matching.
            name, description = (
                await session.exec(
                    select(EntityKind.name, EntityKind.description)
                    .where(EntityKind.structural.is_(False))
                    .limit(1)
                )
            ).one()
        entity = ExtractedEntity(
            name="Something", type=ontology.CONCEPT, suggested_type=description
        )
        resolved = (await prepare_entities([entity]))[0].type
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
            before = await session.scalar(select(func.count()).select_from(EntityKind))
        resolved = (
            await prepare_entities(
                [
                    ExtractedEntity(
                        name="Something",
                        type=ontology.CONCEPT,
                        suggested_type=str(uuid.uuid4()),
                    )
                ]
            )
        )[0].type
        async with dbutil.actor(owner) as session:
            after = await session.scalar(select(func.count()).select_from(EntityKind))
        return resolved, before or 0, after or 0

    resolved, before, after = dbutil.run(body())
    assert resolved == ontology.CONCEPT
    assert after == before


def test_prepare_entities_falls_back_to_concept_without_ontology_vectors(
    fake_embedder: RecordingEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        build.ontology, "current", lambda: SimpleNamespace(entity_description_vectors={})
    )
    entity = ExtractedEntity(
        name="Something", type=ontology.CONCEPT, suggested_type="a novel kind"
    )

    async def body() -> str:
        return (await prepare_entities([entity]))[0].type

    assert dbutil.run(body()) == ontology.CONCEPT  # no vectors to score means the concept stands


def test_build_graph_writes_a_slice_then_resumes(
    fake_llm: FakeLLM, fake_embedder: RecordingEmbedder, fake_gate: FakeGate
) -> None:
    snapshot = ontology.current()
    fake_llm.register(
        snapshot.llm_extraction,
        snapshot.llm_extraction(
            e=[
                snapshot.llm_entity(n="Ada", t="author"),
                snapshot.llm_entity(n="Notes", t="concept"),
            ],
            f=[snapshot.llm_fact(s="Ada", p="related_to", o="Notes", statement="Ada keeps notes")],
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


def test_build_graph_source_filter_drops_a_path_and_skips_a_ghost_subject(
    fake_llm: FakeLLM, fake_embedder: RecordingEmbedder, fake_gate: FakeGate
) -> None:
    snapshot = ontology.current()
    fake_llm.register(
        snapshot.llm_extraction,
        snapshot.llm_extraction(
            e=[
                snapshot.llm_entity(n="Ada", t="author"),
                snapshot.llm_entity(n="Ada", t="author"),
                snapshot.llm_entity(n="notes/graph_rag.md", t="concept"),
            ],
            f=[
                snapshot.llm_fact(s="Ada", p="related_to", statement="Ada keeps notes"),
                snapshot.llm_fact(s="ghost", p="related_to", statement="ghost drifts"),
            ],
        ),
    )

    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE, title="alpha source")
        await seedgraph.seed_chunk(owner, LONG_PROSE, title="beta other")
        return await build_graph(scopes=frozenset({owner}), source="alpha")

    assert dbutil.run(body()) == (1, 1)  # Ada minted once, path dropped, ghost fact skipped


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
    journal_text = "#project\n- 2024-01-01: shipped the first release"

    async def body() -> tuple[int, int, int]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, journal_text, title="My Project")
        entities, facts = await build_graph(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            projects = (
                await session.exec(
                    select(func.count())
                    .select_from(EntityContent)
                    .where(EntityContent.type == ontology.PROJECT)
                )
            ).one()
        return entities, facts, projects or 0

    entities, facts, projects = dbutil.run(body())
    assert entities >= 1 and facts >= 1
    assert projects == 1  # the title entity was lifted to a Project node


def test_build_graph_leaves_a_chunk_pending_on_a_timeout(
    fake_gate: FakeGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_raising_client(
        monkeypatch, APITimeoutError(request=httpx.Request("POST", "http://llm.invalid"))
    )

    async def body() -> tuple[tuple[int, int], bool]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        result = await build_graph(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            done = await session.get(seedgraph.Chunk, chunk)
        return result, done is not None and done.processed_at is not None

    result, marked = dbutil.run(body())
    assert result == (0, 0)
    assert marked is False  # the chunk stays pending for a retry


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

    with pytest.raises(ExtractionUnreachableError):
        dbutil.run(body())


@pytest.mark.parametrize("kind", ["length", "validation"])
def test_build_graph_marks_processed_on_an_unfinishable_extraction(
    kind: str, fake_gate: FakeGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    if kind == "length":
        error: BaseException = LengthFinishReasonError.__new__(LengthFinishReasonError)
    else:
        try:
            ontology.current().llm_extraction(e="truncated", f=[])
            raise AssertionError("expected a ValidationError")
        except ValidationError as caught:
            error = caught
    install_raising_client(monkeypatch, error)

    async def body() -> tuple[tuple[int, int], bool]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        result = await build_graph(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            done = await session.get(seedgraph.Chunk, chunk)
        return result, done is not None and done.processed_at is not None

    result, marked = dbutil.run(body())
    assert result == (0, 0)
    assert marked is True  # marked done despite the overflow, never left to loop


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
            survivors = list(await session.exec(select(EntityContent.id)))
            subject = (
                await session.exec(select(FactContent.subject_id).where(FactContent.id == fact))
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
            facts = (await session.exec(select(func.count()).select_from(FactContent))).one()
            entities = (await session.exec(select(func.count()).select_from(EntityContent))).one()
        return facts or 0, entities or 0

    facts, entities = dbutil.run(body())
    assert facts == 0  # the dangling fact is dropped, never repointed to nothing
    assert entities == (1 if with_object else 0)  # only an ordinary object node survives


@pytest.mark.parametrize(
    ("sibling", "expected_type"),
    [("#project overview span", ontology.PROJECT), ("ordinary prose", ontology.CONCEPT)],
    ids=["declaring-sibling", "plain-siblings"],
)
def test_journal_extraction_borrows_a_declared_type_from_a_sibling_chunk(
    sibling: str, expected_type: str
) -> None:
    owner = uuid.uuid4()
    chunk = Chunk(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        ord=1,
        text="- 2024-01-01: shipped the first release",
        created_by=owner,
        scopes=[owner],
    )
    document = Document(content_hash="h", created_by=owner, scopes=[owner], title="My Project")

    class SiblingSession:
        async def exec(self, statement: object) -> list[str]:
            return [sibling, chunk.text]

    async def body() -> tuple[list[ExtractedEntity], list[TimedFact]]:
        return await journal_extraction(cast("Session", SiblingSession()), chunk, document)

    entities, facts = dbutil.run(body())
    assert [entity.type for entity in entities] == [expected_type]  # own tag absent, sibling wins
    assert len(facts) == 1 and facts[0].subject == "My Project"


def test_raise_failures_groups_multiple_unexpected_chunk_errors() -> None:
    chunks = [
        Chunk(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            ord=index,
            text="x",
            created_by=uuid.uuid4(),
            scopes=[uuid.uuid4()],
        )
        for index in range(3)
    ]
    results: list[set[uuid.UUID] | BaseException] = [set(), RuntimeError("a"), ValueError("b")]

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
                identity=fact_id(fact.subject, fact.predicate, fact.object_, fact.statement),
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
                select(FactContent.statement, FactClaim.recorded, FactClaim.valid)
                .join(FactClaim, FactClaim.content_id == FactContent.id)
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
                                fact.subject,
                                fact.predicate,
                                fact.object_,
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
            claim = await session.get(FactClaim, claim_id, execution_options=GATE_OFF)
            assert claim is not None and claim.valid is not None
            return claim.valid.is_empty

    assert dbutil.run(body())


def test_write_graph_slice_retries_a_transient_db_conflict(
    fixed_embedder: FixedEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"n": 0}
    real_resolve = build.resolve_entities

    async def flaky(writer: GraphWriter, entities: list[PreparedEntity]) -> dict[str, uuid.UUID]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DBAPIError("stmt", None, WrappedDBAPI(TransactionRollbackError("deadlock")))
        return await real_resolve(writer, entities)

    monkeypatch.setattr(build, "resolve_entities", flaky)

    async def body() -> set[uuid.UUID]:
        owner = await seedgraph.fresh_owner()
        chunk_id = await seedgraph.seed_chunk(owner, LONG_PROSE)
        chunk = Chunk(
            id=chunk_id,
            document_id=uuid.uuid4(),
            ord=0,
            text=LONG_PROSE,
            created_by=owner,
            scopes=[owner],
        )
        async with session_for(dbutil.actor(owner)) as opened:
            return await write_graph_slice(
                opened,
                chunk,
                [ExtractedEntity(name="Ada", type="author")],
                [],
            )

    touched = dbutil.run(body())
    assert attempts["n"] == 2  # the first transient conflict retried into a clean write
    assert len(touched) == 1  # the retried attempt resolved and returned the Ada entity
