import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from doubles import RecordingEmbedder, install_fake_embedder
from factories import build_live_fact
from graphdb import DB_UP, FakeLLM, add_principals, drop_principals, purge_owner
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from strategies import short_text

from aizk.config import settings
from aizk.extract.llm import decide_consolidation
from aizk.extract.llm import triples as triples_module
from aizk.extract.models import ConsolidationVerdict, ExtractedFact, TimedFact
from aizk.graph.build import GraphWriter
from aizk.graph.ids import fact_id
from aizk.store import (
    Chunk,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    acting_as,
)


@given(
    statements=st.lists(short_text, min_size=0, max_size=5, unique=True),
    proposed=st.integers(min_value=-1, max_value=4),
)
def test_decide_keeps_only_a_supersedes_inside_the_candidate_set(
    monkeypatch: pytest.MonkeyPatch, statements: list[str], proposed: int
) -> None:
    """An empty set is a trivial ADD, an UPDATE keeps its supersedes only if it names a candidate.

    With nothing to compare against the verdict is an ADD and no model call is made. Otherwise the
    model is faked to claim an UPDATE superseding a chosen id, in the candidate set when the index
    is valid and a fresh unrelated id otherwise, so the verdict keeps the id only when it names a
    claim actually offered, the guard against a hallucinated id retiring a real claim.
    """
    existing = [build_live_fact(id=uuid.uuid4(), statement=statement) for statement in statements]
    in_set = 0 <= proposed < len(existing)
    target = existing[proposed].id if in_set else uuid.uuid4()
    calls: list[bool] = []

    async def fake_structured(system: str, user: str, schema: type) -> object:
        calls.append(True)
        return ConsolidationVerdict(action="UPDATE", supersedes=target)

    monkeypatch.setattr(triples_module, "structured", fake_structured)
    candidate = ExtractedFact(subject="s", predicate="related_to", statement="new")
    verdict = asyncio.run(decide_consolidation(candidate, existing))

    if not existing:
        assert verdict.action == "ADD" and verdict.supersedes is None
        assert calls == []  # the trivial ADD never reaches the model
    else:
        assert verdict.action == "UPDATE"
        assert verdict.supersedes == (target if in_set else None)


async def seed_subject(owner: uuid.UUID, name: str) -> uuid.UUID:
    """Plant one subject entity content the consolidation resolves its fact against, return its id.

    owner: principal that owns the claim.
    name: surface form the consolidated fact's subject matches on.
    """
    subject = uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(EntityContent(id=subject, name=name, type="Concept", embedding=None))
        await session.flush()
        session.add(EntityClaim(content_id=subject, owner_id=owner))
    return subject


async def seed_chunk(owner: uuid.UUID) -> uuid.UUID:
    """Plant a document and one chunk so a consolidated fact has a real provenance to point at.

    owner: principal that owns the document and chunk.
    """
    document, chunk = uuid.uuid4(), uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(
            Document(id=document, content_hash="consolidate", owner_id=owner, title="source")
        )
        session.add(Chunk(id=chunk, document_id=document, ord=0, text="span", owner_id=owner))
    return chunk


@pytest.mark.usefixtures("fake_embedder")
def test_consolidate_gate_offers_only_the_open_window_candidate(
    fresh_principal: uuid.UUID, fake_llm: FakeLLM
) -> None:
    """Only the open latest claim reaches the verdict, the closed and future ones the gate hides.

    All three rows carry an open `recorded`, so an open transaction-time range alone would offer
    every one as a supersession candidate, and the catalog the consolidation prompt is handed
    proves the `valid`-time gate, not `recorded` alone, decides which claims are weighed.
    """
    owner = fresh_principal

    now = datetime.now(UTC)
    windows = {
        "open": (now - timedelta(hours=2), None),
        "closed": (now - timedelta(hours=2), now - timedelta(minutes=30)),
        "future": (now + timedelta(hours=2), None),
    }
    ids = {name: uuid.uuid4() for name in windows}

    async def probe() -> str:
        subject = await seed_subject(owner, "Subject")
        async with acting_as(owner) as session:
            for name, (valid_from, valid_to) in windows.items():
                content = uuid.uuid4()
                session.add(
                    FactContent(
                        id=content,
                        subject_id=subject,
                        predicate="related_to",
                        statement=f"{name} fact",
                        embedding=None,
                    )
                )
                await session.flush()
                session.add(
                    FactClaim(
                        id=ids[name],
                        content_id=content,
                        owner_id=owner,
                        valid=Range(valid_from, valid_to),
                    )
                )
        chunk = await seed_chunk(owner)
        candidate = TimedFact(subject="Subject", predicate="related_to", statement="the candidate")
        async with acting_as(owner) as session:
            await GraphWriter(session, owner, None).consolidate(candidate, chunk)
        verdict_calls = [
            call
            for call in fake_llm.completions.calls
            if call["response_model"] is ConsolidationVerdict
        ]
        return str(verdict_calls[-1]["messages"])

    catalog = asyncio.run(probe())
    assert str(ids["open"]) in catalog
    assert str(ids["closed"]) not in catalog
    assert str(ids["future"]) not in catalog


# a small statement pool keeps the live set under settings.similar_facts (5 by default), so a
# chosen supersedes always names a real candidate, and re-drawing a statement exercises the
# content-id duplicate skip

STATEMENTS = ["alpha holds", "beta holds", "gamma holds", "delta holds"]
statements = st.sampled_from(STATEMENTS)


def content_id(subject: str, statement: str) -> uuid.UUID:
    """The content-addressed id `GraphWriter.consolidate` mints for a statement of the given
    subject.

    entity_id/fact_id hash only their content, not the owner, so two different owners writing the
    same subject and statement text would otherwise mint the identical primary key and collide on
    insert under row level security, which scopes visibility but not this global identity space.
    Each machine instance is keyed to its own owner-derived subject name (see `subject_name`) so
    its content ids never collide with another run's, independent runs being the only property
    this machine tests, not the cross-tenant collision that scoping by owner sidesteps here.

    subject: subject entity name the fact is filed under.
    statement: the fact statement whose stable id the model mirrors.
    """
    return fact_id(subject, "related_to", "", statement)


class ConsolidateMachine(RuleBasedStateMachine):
    """A fact timeline driving `GraphWriter.consolidate` through ADD, UPDATE, NOOP, and the
    duplicate skip.

    A reference model mirrors the live set (the latest open-window claims) and the full history
    count, and after every step the real live count and the history count are asserted equal to
    it. ADD inserts a new statement, UPDATE retires a named live claim then inserts, NOOP and a
    re-drawn statement leave the graph untouched, so a sequence proves the ADD-UPDATE-NOOP
    contract and the content-id idempotence the example tests pinned one outcome each.
    """

    def __init__(self) -> None:
        super().__init__()
        self.owner = uuid.uuid4()
        self.subject = uuid.uuid4()
        self.subject_name = f"Subject-{self.owner.hex}"
        self.chunk = uuid.uuid4()
        self.live: set[uuid.UUID] = set()  # content ids currently claimed live, open window
        self.history: set[uuid.UUID] = set()  # every content id ever written, the dedup key set
        self.previous_embedder: RecordingEmbedder | None = None
        self.patcher = pytest.MonkeyPatch()
        # the verdict the faked model returns for the next consolidate, set per rule
        self.verdict = ConsolidationVerdict(action="ADD")

    @initialize()
    def seed(self) -> None:
        """Install the fake embed and verdict seams and plant the principal, subject, and chunk."""
        self.previous_embedder = install_fake_embedder(RecordingEmbedder())
        self.patcher.setattr(triples_module, "structured", self.fake_structured)
        asyncio.run(self.prepare())

    async def fake_structured(
        self, system: str, user: str, schema: type[ConsolidationVerdict]
    ) -> ConsolidationVerdict:
        """Return the verdict this step staged, the consolidation model stubbed at its seam.

        system: the consolidation prompt, ignored since the verdict is staged.
        user: the rendered candidate-and-catalog body, ignored for the same reason.
        schema: the response model the caller asks for, always ConsolidationVerdict here.
        """
        return self.verdict

    @rule(statement=statements)
    def add(self, statement: str) -> None:
        """An ADD inserts a fresh statement, a re-drawn one is the content-id duplicate skip."""
        identity = content_id(self.subject_name, statement)
        self.verdict = ConsolidationVerdict(action="ADD")
        asyncio.run(self.run_consolidate(statement))
        if identity not in self.history:
            self.history.add(identity)
            self.live.add(identity)

    @precondition(lambda self: bool(self.live))
    @rule(statement=statements)
    def noop(self, statement: str) -> None:
        """A NOOP verdict over a non-empty live set writes nothing, the graph left as it was.

        A new statement is dropped by the verdict and a re-drawn one is the content-id skip, so
        either way the model is unchanged and the invariant pins the graph to its prior counts.
        """
        self.verdict = ConsolidationVerdict(action="NOOP")
        asyncio.run(self.run_consolidate(statement))

    @precondition(lambda self: bool(self.live))
    @rule(statement=statements, data=st.data())
    def update(self, statement: str, data: st.DataObject) -> None:
        """An UPDATE retires a named live claim, closing its `recorded`, then inserts the new
        one.

        `verdict.supersedes` names a claim id, not a content id: `GraphWriter.consolidate` looks
        the id up as `FactClaim`, the per-container bi-temporal row the content/claim split moved
        `is_current` and retirement onto, so the chosen content's current live claim id is read
        back from the database rather than reused directly the way the pre-split single-table
        identity let the model draw straight from its own content-id set.
        """
        identity = content_id(self.subject_name, statement)
        superseded_content = data.draw(st.sampled_from(sorted(self.live, key=str)))
        superseded_claim = asyncio.run(self.live_claim_id(superseded_content))
        self.verdict = ConsolidationVerdict(action="UPDATE", supersedes=superseded_claim)
        asyncio.run(self.run_consolidate(statement))
        if identity in self.history:  # the new statement already exists, the duplicate skip wins
            return
        self.history.add(identity)
        self.live.discard(superseded_content)
        self.live.add(identity)

    async def live_claim_id(self, content: uuid.UUID) -> uuid.UUID:
        """The current live claim id staking this content, the id `verdict.supersedes` must name.

        content: the fact content id whose live claim is looked up.
        """
        async with acting_as(self.owner) as session:
            claim_id = await session.scalar(
                select(FactClaim.id).where(FactClaim.content_id == content)
            )
        assert claim_id is not None  # content drawn from self.live always carries an open claim
        return claim_id

    @invariant()
    def the_graph_matches_the_model(self) -> None:
        """The live count and the full history count read back exactly what the model predicts."""
        live, total = asyncio.run(self.read_counts())
        assert live == len(self.live)
        assert total == len(self.history)

    def teardown(self) -> None:
        """Restore the patched seams and delete every row this run created."""
        self.patcher.undo()
        install_fake_embedder(self.previous_embedder)
        asyncio.run(self.purge())

    async def prepare(self) -> None:
        """Seed the principal, the subject entity content, and the provenance chunk."""
        await add_principals(self.owner)
        async with acting_as(self.owner) as session:
            session.add(
                EntityContent(
                    id=self.subject, name=self.subject_name, type="Concept", embedding=None
                )
            )
            await session.flush()
            session.add(EntityClaim(content_id=self.subject, owner_id=self.owner))
            document = uuid.uuid4()
            session.add(
                Document(id=document, content_hash="machine", owner_id=self.owner, title="src")
            )
            session.add(
                Chunk(id=self.chunk, document_id=document, ord=0, text="span", owner_id=self.owner)
            )

    async def run_consolidate(self, statement: str) -> None:
        """Run the real `GraphWriter.consolidate` for one statement under the owning principal."""
        candidate = TimedFact(
            subject=self.subject_name, predicate="related_to", statement=statement
        )
        async with acting_as(self.owner) as session:
            await GraphWriter(session, self.owner, None).consolidate(candidate, self.chunk)

    async def read_counts(self) -> tuple[int, int]:
        """Read the live claim count and the full history claim count of the subject."""
        async with acting_as(self.owner) as session:
            base = (
                select(func.count())
                .select_from(FactClaim)
                .join(FactContent, FactContent.id == FactClaim.content_id)
                .where(FactContent.subject_id == self.subject)
            )
            live = await session.scalar(base)
            total = await session.scalar(base.execution_options(**{settings.skip_live_gate: True}))
        return live or 0, total or 0

    async def purge(self) -> None:
        """Remove the subject's facts, the subject, and the principal this run created."""
        await purge_owner(self.owner)
        await drop_principals(self.owner)


ConsolidateMachine.TestCase.settings = hypothesis_settings(
    max_examples=15,
    stateful_step_count=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
TestConsolidateMachine = pytest.mark.skipif(not DB_UP, reason="aizk postgres not reachable")(
    ConsolidateMachine.TestCase
)
