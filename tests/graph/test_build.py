import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import dbutil
import httpx
import pytest
import seedgraph
from asyncpg.exceptions import TransactionRollbackError
from doubles import FakeLLM, RecordingEmbedder, install_fake_embedder
from openai import APIConnectionError, APITimeoutError, LengthFinishReasonError
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError

from aizk.config import settings
from aizk.exceptions import ExtractionUnreachableError
from aizk.extract import ontology
from aizk.extract.llm import client as llm_client
from aizk.extract.models import (
    BatchConsolidationVerdict,
    ConsolidationVerdict,
    ExtractedEntity,
    TimedFact,
)
from aizk.graph.build import (
    GraphWriter,
    build_graph,
    dedup_entities,
    is_transient_db_error,
    redirect_entity,
    resolve_entity_type,
)
from aizk.graph.ids import entity_id
from aizk.serving import EntityGate
from aizk.serving.embed import EmbedMode
from aizk.store import EntityContent, EntityKind, FactClaim, FactContent, LiveFact, acting_as

pytestmark = pytest.mark.usefixtures("migrated_db")

# a chunk long enough to clear extract_min_chars, so `extract_and_consolidate` runs the LLM path
# rather than short-circuiting; every build-graph test that wants extraction seeds this text.
LONG_PROSE = "Ada Lovelace keeps detailed notes about memory and computation across her notebooks."
GATE_OFF = {settings.skip_live_gate: True}

# two unit vectors whose cosine similarity is exactly 0.8, engineered so the fixed embedder below
# lands a candidate squarely in the consolidation borderline band `[floor 0.75, auto 0.9)` against
# a pool fact carrying `E_BAND`, or on an auto-merge (cosine 1.0) against one carrying `E0`.
E0 = [1.0] + [0.0] * (settings.embed_dim - 1)
E_BAND = [0.8, 0.6] + [0.0] * (settings.embed_dim - 2)


class FixedEmbedder(RecordingEmbedder):
    """A recording embedder returning one fixed vector for every text, so cosine is fully scripted.

    The real deterministic double hashes each text to an unpredictable vector; consolidation tests
    instead need a candidate statement to sit at a known cosine from a seeded pool fact, so this
    returns a caller-chosen vector regardless of input while still recording every call.

    vector: the dense vector every `embed` call hands back, one copy per input text.
    """

    def __init__(self, vector: list[float]) -> None:
        super().__init__()
        self.vector = vector

    async def embed(self, texts: list[str], mode: EmbedMode = "document") -> list[list[float]]:
        """Record the call and return the one fixed vector per input text.

        texts: input strings to embed.
        mode: query or document, recorded for symmetry with the real double.
        """
        self.calls.append((list(texts), mode))
        return [list(self.vector) for _ in texts]


class FakeGate:
    """A stand-in for the GLiNER2 relevance gate whose `relevant` verdict a test dictates.

    Installed on `EntityGate.singleton_instance` the same way the embedder double is, so
    `llm_extraction`'s `EntityGate().relevant(...)` call resolves here with no torch or checkpoint.

    result: the fixed relevance verdict every `relevant` call returns.
    """

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[str] = []

    def relevant(self, text: str) -> bool:
        """Record the scored text and return the dictated verdict.

        text: chunk span the real gate would score against the ontology labels.
        """
        self.calls.append(text)
        return self.result


class RaisingCompletions:
    """A `chat.completions` stand-in whose `parse` always raises, to drive `llm_extraction`'s arms.

    error: the exception every `parse` call raises, an openai SDK error or a bare failure.
    """

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
        """Raise the configured error instead of returning a parsed completion.

        model: chat model id the seam sent, accepted and ignored.
        messages: the assembled system-then-user pair, accepted and ignored.
        response_format: schema the caller asked for, accepted and ignored.
        temperature: sampling temperature, accepted and ignored.
        timeout: per-call ceiling, accepted and ignored.
        max_tokens: output token cap, accepted and ignored.
        extra_body: provider extra_body, accepted and ignored.
        """
        raise self.error


class RaisingClient:
    """An AsyncOpenAI stand-in whose only reachable path, `chat.completions.parse`, raises.

    error: the exception the nested completions stand-in raises on every call.
    """

    def __init__(self, error: BaseException) -> None:
        self.chat = type("Chat", (), {"completions": RaisingCompletions(error)})()


@pytest.fixture
def fake_gate() -> Iterator[FakeGate]:
    """Install a controllable relevance gate on `EntityGate`, cleared after the test.

    Yields the gate so a test can flip `.result` to exercise the gated-out branch; restores the
    singleton slot on exit the way the embedder double does.
    """
    gate = FakeGate()
    previous = EntityGate.__dict__.get("singleton_instance")
    EntityGate.singleton_instance = gate
    yield gate
    if "singleton_instance" in EntityGate.__dict__:
        delattr(EntityGate, "singleton_instance")
    if previous is not None:
        EntityGate.singleton_instance = previous


@pytest.fixture
def fixed_embedder() -> Iterator[FixedEmbedder]:
    """Install a fixed-vector embedder (candidate statements embed to `E0`), cleared after."""
    embedder = FixedEmbedder(E0)
    install_fake_embedder(embedder)
    yield embedder
    install_fake_embedder(None)


def install_raising_client(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """Route every structured LLM call through a client whose `parse` raises `error`.

    monkeypatch: the test's patcher, auto-reverted on teardown.
    error: the exception the fake client raises inside `combined_extract`.
    """
    client = RaisingClient(error)
    monkeypatch.setattr(llm_client.LLMClientPool, "client_for", lambda self, *a, **k: client)


class WrappedDBAPI(Exception):
    """A two-layer wrapper mirroring how asyncpg surfaces a rollback under SQLAlchemy's dialect.

    `is_transient_db_error` reads `error.orig.orig`, so the DBAPIError's own `orig` must itself
    carry an `orig` holding the real asyncpg error; this is that middle layer.

    inner: the innermost error `DBAPIError.orig.orig` exposes.
    """

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
    """Only a doubly-wrapped `TransactionRollbackError` counts as the retryable transient conflict.

    A deadlock or serialization failure arrives as `DBAPIError.orig.orig`; any other DBAPI cause
    and any non-DBAPI error are not the contention `write_graph_slice` retries, so both are false.
    """
    assert is_transient_db_error(error) is expected


def test_redirect_entity_resolves_null_absent_replaced_and_dropped() -> None:
    """The four redirect cases: a null id, an untouched id, a replaced duplicate, a dropped one.

    A null passes through and never drops, an id the merge never touched passes through unchanged,
    a duplicate resolves to its canonical replacement, and one mapped to null reports the drop the
    caller reads to delete the dangling fact rather than repoint it to nothing.
    """
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
    """Resolution mints a fresh node, reuses an exact claim, folds a near match, or drops a path.

    insert: an unseen name mints a content-addressed node a second resolve reuses off its own
    claim with no embed. exact: a name this container already claims returns it with no fuzzy
    search. fuzzy: a fresh name whose embedding equals a stored entity's vector folds onto that
    neighbor under the threshold. path: a name that normalizes to nothing folds away with a null.
    """

    async def body() -> uuid.UUID | None:
        owner = await seedgraph.fresh_owner()
        async with acting_as(owner) as session:
            if scenario == "exact":
                await seedgraph.add_entity(
                    session,
                    owner,
                    "Exact Fixture",
                    type="Author",
                    content_id=entity_id("Exact Fixture", "Author"),
                )
            if scenario == "fuzzy":
                await seedgraph.add_entity(
                    session,
                    owner,
                    "Existing",
                    type="Concept",
                    embedding=deterministic("document:Newcomer"),
                    content_id=entity_id("Existing", "Concept"),
                )
        async with acting_as(owner) as session:
            writer = GraphWriter(session, owner, ())
            if scenario == "insert":
                first = await writer.resolve("Brand New", "Concept")
                second = await writer.resolve("Brand New", "Concept")
                assert first == second  # the second resolve reuses off the minted claim
                return first
            if scenario == "exact":
                return await writer.resolve("Exact Fixture", "Author")
            if scenario == "fuzzy":
                return await writer.resolve("Newcomer", "Concept")
            return await writer.resolve("notes/graph_rag.md", "Concept")

    expected = {
        "insert": entity_id("Brand New", "Concept"),
        "exact": entity_id("Exact Fixture", "Author"),
        "fuzzy": entity_id("Existing", "Concept"),
        "path": None,
    }
    assert dbutil.run(body()) == expected[scenario]


def deterministic(text: str) -> list[float]:
    """The recording embedder's own vector for a text, so a seeded row matches a later embed."""
    from doubles import deterministic_vector

    return deterministic_vector(text, settings.embed_dim)


@pytest.mark.parametrize("scenario", ["add", "noop", "update"])
def test_consolidate_applies_verdict(scenario: str, fixed_embedder: FixedEmbedder) -> None:
    """A rule-decided ADD mints a claim, a NOOP writes nothing, and an UPDATE retires the old one.

    add: no existing pool is a trivial ADD leaving one live claim. noop: a candidate identical to
    a live claim of the same predicate and object is a near-duplicate, so nothing new lands.
    update: the same predicate with a different object supersedes the live claim, closing it and
    leaving the new one live, two claims in history and one live.
    """
    now = datetime.now(UTC)

    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with acting_as(owner) as session:
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
        async with acting_as(owner) as session:
            await GraphWriter(session, owner, ()).consolidate_facts([fact], resolved, chunk)
        async with acting_as(owner) as session:
            total = await session.scalar(
                select(func.count()).select_from(FactClaim).execution_options(**GATE_OFF)
            )
            live = await session.scalar(
                select(func.count()).select_from(LiveFact).where(LiveFact.subject_id == subject)
            )
        return total or 0, live or 0

    assert dbutil.run(body()) == {"add": (1, 1), "noop": (1, 1), "update": (2, 1)}[scenario]


def test_consolidate_is_idempotent_and_reads_an_empty_pool(fixed_embedder: FixedEmbedder) -> None:
    """Re-consolidating an already-claimed fact writes nothing, the cascade's first free tier.

    The first pass adds the claim; the second finds the identity already claimed, drops the
    candidate, and returns before any embed or pool read, so the claim count stays one. An empty
    subject set short-circuits `live_facts_by_subject` to an empty map with no query at all.
    """

    async def body() -> tuple[dict[uuid.UUID, list[LiveFact]], int]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with acting_as(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
        fact = TimedFact(subject="Subject", predicate="related_to", statement="only")
        async with acting_as(owner) as session:
            writer = GraphWriter(session, owner, ())
            empty = await writer.live_facts_by_subject(set())
            await writer.consolidate_facts([fact], {"Subject": subject}, chunk)
        async with acting_as(owner) as session:
            await GraphWriter(session, owner, ()).consolidate_facts(
                [fact], {"Subject": subject}, chunk
            )
        async with acting_as(owner) as session:
            total = await session.scalar(
                select(func.count()).select_from(FactClaim).execution_options(**GATE_OFF)
            )
        return empty, total or 0

    empty, total = dbutil.run(body())
    assert empty == {}  # no subjects means no pool query and an empty map
    assert total == 1  # the second consolidation added no second claim


def test_consolidate_defers_borderline_facts_to_the_batch(
    fixed_embedder: FixedEmbedder, fake_llm: FakeLLM
) -> None:
    """Two facts in the ambiguous cosine band defer to one batched call and both land as claims.

    Each candidate's top match sits at cosine 0.8, inside `[floor, auto)`, so the rule tier defers
    both to `decide_consolidations_batch`. The batch's UPDATE names a claim outside the candidate's
    own pool, so the hallucinated supersedes is dropped and the write proceeds as a plain insert;
    the ADD lands too, and one writer resolves the review stamp once for both.
    """
    phantom = uuid.uuid4()

    async def body() -> int:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        async with acting_as(owner) as session:
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
        async with acting_as(owner) as session:
            await GraphWriter(session, owner, ()).consolidate_facts(
                facts, {"Subject": subject}, chunk
            )
        async with acting_as(owner) as session:
            return (
                await session.scalar(
                    select(func.count()).select_from(FactClaim).execution_options(**GATE_OFF)
                )
                or 0
            )

    assert dbutil.run(body()) == 3  # one seeded plus the two borderline candidates


@pytest.mark.parametrize("scenario", ["absent", "clamp", "forward", "no_lower"])
def test_close_superseded_claim_clamps_and_tolerates_a_vanished_target(scenario: str) -> None:
    """Closing a superseded claim clamps a backdated start and no-ops when the target is gone.

    absent: a supersedes id no row carries returns cleanly, the race-safe defense. clamp: a start
    earlier than the retired claim's own lower bound clamps up to it, an immediately-closed window
    rather than an inverted range. forward: a later start closes at that start. no_lower: an
    undated retired claim closes at the write time with an open lower bound.
    """
    base = datetime.now(UTC)

    async def body() -> tuple[bool, datetime | None, datetime | None] | None:
        owner = await seedgraph.fresh_owner()
        supersedes = uuid.uuid4()
        async with acting_as(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
            if scenario != "absent":
                _, supersedes = await seedgraph.add_fact(
                    session,
                    owner,
                    subject,
                    statement="retired",
                    valid=None if scenario == "no_lower" else Range(base, None),
                )
        # the close write time is now, after the seed landed, so it never predates the retired
        # claim's own `recorded` lower bound; the backdated correction lives in `valid_from`.
        now = datetime.now(UTC)
        valid_from = {
            "clamp": base - timedelta(days=10),
            "forward": base + timedelta(days=10),
        }.get(scenario)
        async with acting_as(owner) as session:
            writer = GraphWriter(session, owner, ())
            await writer.close_superseded_claim(supersedes, valid_from, now)
            retired = await session.get(FactClaim, supersedes, execution_options=GATE_OFF)
            if retired is None:
                return None
            return retired.recorded.upper_inf, retired.valid.lower, retired.valid.upper

    result = dbutil.run(body())
    if scenario == "absent":
        assert result is None  # a vanished target closes nothing and never raises
        return
    recorded_open, lower, upper = result
    assert recorded_open is False  # the retired claim left the live set
    if scenario == "clamp":
        assert upper == lower  # the backdated start clamped to the retired lower bound
    elif scenario == "forward":
        assert upper is not None and upper > lower  # closed at the later valid start
    else:
        assert lower is None and upper is not None  # undated retired claim, open lower bound


def test_resolve_entity_type_passes_through_a_confident_type_unchanged(
    fake_embedder: RecordingEmbedder,
) -> None:
    """An entity the extractor typed confidently never touches the auto-create cascade at all."""
    entity = ExtractedEntity(name="Ada", type="Author")

    async def body() -> str:
        async with acting_as(await seedgraph.fresh_owner()) as session:
            return await resolve_entity_type(GraphWriter(session, uuid.uuid4(), ()), entity)

    assert dbutil.run(body()) == "Author"


def test_resolve_entity_type_grows_the_catalog_for_a_concept_fallback_with_a_suggestion(
    fake_embedder: RecordingEmbedder,
) -> None:
    """A Concept fallback carrying a suggestion resolves through the auto-create cascade, folding
    into the identical description it names rather than staying Concept."""

    async def body() -> tuple[str, str]:
        owner = await seedgraph.fresh_owner()
        async with acting_as(owner) as session:
            await ontology.refresh(session)
            name, description = (
                await session.execute(select(EntityKind.name, EntityKind.description).limit(1))
            ).one()
            entity = ExtractedEntity(
                name="Something", type=ontology.CONCEPT, suggested_type=description
            )
            resolved = await resolve_entity_type(GraphWriter(session, owner, ()), entity)
        return resolved, name

    try:
        resolved_type, existing_name = dbutil.run(body())
        assert resolved_type == existing_name
    finally:
        dbutil.run(dbutil.admin_exec("DELETE FROM entity_kind WHERE domain = 'auto'"))

        async def restore() -> None:
            async with acting_as(await seedgraph.fresh_owner()) as session:
                await ontology.refresh(session)

        dbutil.run(restore())


def test_build_graph_writes_a_slice_then_resumes(
    fake_llm: FakeLLM, fake_embedder: RecordingEmbedder, fake_gate: FakeGate
) -> None:
    """The first pass mints the extracted entities and fact, the second finds the chunk done.

    A chunk stays pending until its `processed_at` is stamped, so a build resumed after the slice
    landed reprocesses nothing and reports a zero delta the second time.
    """
    snapshot = ontology.current()
    fake_llm.register(
        snapshot.llm_extraction,
        snapshot.llm_extraction(
            e=[
                snapshot.llm_entity(n="Ada", t="Author"),
                snapshot.llm_entity(n="Notes", t="Concept"),
            ],
            f=[snapshot.llm_fact(s="Ada", p="related_to", o="Notes", statement="Ada keeps notes")],
        ),
    )

    async def body() -> tuple[tuple[int, int], tuple[int, int]]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE)
        first = await build_graph(principal_id=owner)
        second = await build_graph(principal_id=owner)
        return first, second

    first, second = dbutil.run(body())
    assert first == (2, 1)  # two entities minted, one binary fact between them
    assert second == (0, 0)  # the built chunk is skipped on resume


def test_build_graph_source_filter_drops_a_path_and_skips_a_ghost_subject(
    fake_llm: FakeLLM, fake_embedder: RecordingEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source title selects one chunk, a repeated name reuses its node, a path drops, a ghost
    subject's fact is skipped, leaving one entity and one fact, with the gate disabled."""
    monkeypatch.setattr(settings, "gliner_gate_enabled", False)
    snapshot = ontology.current()
    fake_llm.register(
        snapshot.llm_extraction,
        snapshot.llm_extraction(
            e=[
                snapshot.llm_entity(n="Ada", t="Author"),
                snapshot.llm_entity(n="Ada", t="Author"),
                snapshot.llm_entity(n="notes/graph_rag.md", t="Concept"),
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
        return await build_graph(principal_id=owner, source="alpha")

    assert dbutil.run(body()) == (1, 1)  # Ada minted once, path dropped, ghost fact skipped


def test_build_graph_skips_a_gated_out_chunk(fake_gate: FakeGate) -> None:
    """A chunk the relevance gate rejects marks processed with no extraction call at all."""
    fake_gate.result = False

    async def body() -> tuple[tuple[int, int], bool]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        result = await build_graph(principal_id=owner)
        async with acting_as(owner) as session:
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
    """A sub-minimum prose chunk and an untitled dated line both mark done without an LLM call.

    short-prose: below extract_min_chars with no journal line, nothing to keep. untitled-journal:
    a dated line whose document has no title has no subject to log against, so the journal parse
    yields nothing and the short chunk is stamped done rather than left to loop.
    """

    async def body() -> tuple[tuple[int, int], bool]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, text, title=title)
        result = await build_graph(principal_id=owner)
        async with acting_as(owner) as session:
            done = await session.get(seedgraph.Chunk, chunk)
        return result, done is not None and done.processed_at is not None

    result, marked = dbutil.run(body())
    assert result == (0, 0)
    assert marked is True


def test_journal_line_logs_a_dated_project_fact(fake_embedder: RecordingEmbedder) -> None:
    """A short chunk with a dated journal line and #project logs a dated fact under a Project node.

    The dated line parses deterministically with no LLM call, the #project tag lifts the title
    entity from Concept to Project, and the observes-predicate fact lands under it.
    """
    journal_text = "#project\n- 2024-01-01: shipped the first release"

    async def body() -> tuple[int, int, int]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, journal_text, title="My Project")
        entities, facts = await build_graph(principal_id=owner)
        async with acting_as(owner) as session:
            projects = await session.scalar(
                select(func.count())
                .select_from(EntityContent)
                .where(EntityContent.type == ontology.PROJECT)
            )
        return entities, facts, projects or 0

    entities, facts, projects = dbutil.run(body())
    assert entities >= 1 and facts >= 1
    assert projects == 1  # the title entity was lifted to a Project node


def test_build_graph_leaves_a_chunk_pending_on_a_timeout(
    fake_gate: FakeGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out extraction abandons the chunk and writes nothing, so a later run retries it."""
    install_raising_client(
        monkeypatch, APITimeoutError(request=httpx.Request("POST", "http://llm.invalid"))
    )

    async def body() -> tuple[tuple[int, int], bool]:
        owner = await seedgraph.fresh_owner()
        chunk = await seedgraph.seed_chunk(owner, LONG_PROSE)
        result = await build_graph(principal_id=owner)
        async with acting_as(owner) as session:
            done = await session.get(seedgraph.Chunk, chunk)
        return result, done is not None and done.processed_at is not None

    result, marked = dbutil.run(body())
    assert result == (0, 0)
    assert marked is False  # the chunk stays pending for a retry


def test_build_graph_raises_when_the_endpoint_is_unreachable(
    fake_gate: FakeGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused connection raises `ExtractionUnreachableError` rather than grinding the queue."""
    install_raising_client(
        monkeypatch, APIConnectionError(request=httpx.Request("POST", "http://llm.invalid"))
    )

    async def body() -> None:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE)
        await build_graph(principal_id=owner)

    with pytest.raises(ExtractionUnreachableError):
        dbutil.run(body())


@pytest.mark.parametrize("kind", ["length", "validation"])
def test_build_graph_marks_processed_on_an_unfinishable_extraction(
    kind: str, fake_gate: FakeGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An output too rich to finish inside the token cap marks the chunk processed, not pending.

    length: the openai SDK reports finish_reason length. validation: the same truncation surfaces
    as a raw pydantic ValidationError on an unterminated JSON string under guided decoding. Both
    fail identically on every retry, so the chunk is stamped done rather than left to loop.
    """
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
        result = await build_graph(principal_id=owner)
        async with acting_as(owner) as session:
            done = await session.get(seedgraph.Chunk, chunk)
        return result, done is not None and done.processed_at is not None

    result, marked = dbutil.run(body())
    assert result == (0, 0)
    assert marked is True  # marked done despite the overflow, never left to loop


def test_build_graph_logs_and_skips_an_unexpected_chunk_error(
    fake_gate: FakeGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanticipated per-chunk failure is logged and skipped rather than cancelling the build.

    Unlike an unreachable endpoint, a bare error from one chunk's extraction is not systemic, so
    `raise_unreachable` logs it and moves on, the build returning a zero delta with no raise.
    """
    install_raising_client(monkeypatch, RuntimeError("unexpected"))

    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        await seedgraph.seed_chunk(owner, LONG_PROSE)
        return await build_graph(principal_id=owner)

    assert dbutil.run(body()) == (0, 0)


def test_build_graph_and_dedup_default_to_the_system_principal_on_an_empty_graph() -> None:
    """With no principal both passes act as the system principal and no-op on an empty graph.

    Covers the `principal_id or system` default shared by `build_graph` and `dedup_entities`
    without seeding a chunk, so the gather and the merge each run over nothing and report zero.
    """

    async def body() -> tuple[tuple[int, int], int]:
        await dbutil.reset_db()
        return await build_graph(), await dedup_entities()

    (entities, facts), merged = dbutil.run(body())
    assert (entities, facts) == (0, 0)
    assert merged == 0


def test_dedup_merges_a_slug_twin_and_converges(fake_embedder: RecordingEmbedder) -> None:
    """Two slug spellings of one thing merge to a single node, and a rerun merges nothing more.

    The duplicate's fact content is repointed onto the surviving content before the duplicate is
    deleted, so the second pass finds one canonical node and is a no-op, and the surviving node is
    exactly the one the repointed fact content now names.
    """
    canonical_id = entity_id("Team Memory", "Concept")
    # a duplicate whose id sorts after the canonical's, so `find_duplicates` keeps "Team Memory"
    # and redirects the fact-bearing "team-memory", exercising the repoint rather than the reverse.
    duplicate_id = uuid.UUID(int=canonical_id.int + 1)

    async def body() -> tuple[int, int, int, bool]:
        owner = await seedgraph.fresh_owner()
        async with acting_as(owner) as session:
            await seedgraph.add_entity(session, owner, "Team Memory", content_id=canonical_id)
            await seedgraph.add_entity(session, owner, "team-memory", content_id=duplicate_id)
            fact, _ = await seedgraph.add_fact(
                session, owner, duplicate_id, statement="the duplicate carries a fact"
            )
        first = await dedup_entities(principal_id=owner)
        second = await dedup_entities(principal_id=owner)
        async with acting_as(owner) as session:
            survivors = list(await session.scalars(select(EntityContent.id)))
            subject = await session.scalar(
                select(FactContent.subject_id).where(FactContent.id == fact)
            )
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
    """A path-name entity and any fact naming it are removed outright, not repointed to nothing.

    subject-only: a lone path entity folds away with its own fact. with-object: the fact also
    names an ordinary object, so the same pass leaves that unaffected node untouched while the
    subject leg still drops the dangling fact.
    """

    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        async with acting_as(owner) as session:
            path_like = await seedgraph.add_entity(session, owner, "notes/graph_rag.md")
            object_id = (
                await seedgraph.add_entity(session, owner, "Ordinary Node")
                if with_object
                else None
            )
            await seedgraph.add_fact(
                session, owner, path_like, statement="a dangling fact", object_id=object_id
            )
        await dedup_entities(principal_id=owner)
        async with acting_as(owner) as session:
            facts = await session.scalar(select(func.count()).select_from(FactContent))
            entities = await session.scalar(select(func.count()).select_from(EntityContent))
        return facts or 0, entities or 0

    facts, entities = dbutil.run(body())
    assert facts == 0  # the dangling fact is dropped, never repointed to nothing
    assert entities == (1 if with_object else 0)  # only an ordinary object node survives
