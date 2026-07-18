import asyncio
import functools
from dataclasses import dataclass
from datetime import UTC, datetime

from asyncpg.exceptions import TransactionRollbackError
from loguru import logger
from mainboard.profiling import span
from pydantic import UUID5, UUID7
from sqlalchemy import func, update
from sqlalchemy.exc import DBAPIError
from sqlmodel import select
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from ..config import Settings, settings
from ..extract.dates import with_source_fallback
from ..extract.declaration import SourceDeclaration, journal_facts
from ..extract.extractor import Extractor
from ..extract.models import ExtractedEntity, TimedFact
from ..ontology import Ontology, System
from ..provenance import CaptureContext
from ..serving.embed import EmbedClient, Embedder
from ..serving.extract import LLM, GLiNER
from ..serving.gate import GateClient, RelevanceGate
from ..store import (
    Chunk,
    Document,
    Entity,
    Fact,
)
from ..store.engine import Session
from ..store.identity import User
from ..types import Scopes
from .consolidation import Consolidator
from .grounding import GroundedProjection
from .writer import FactCandidate, FactPlan, GraphWriter, PreparedEntity


@dataclass(frozen=True)
class GraphClients:
    """The model clients one graph build round consumes, threaded from the runtime."""

    extractor: Extractor
    gate: RelevanceGate
    embed: Embedder
    llm: LLM

    @classmethod
    def from_settings(cls, config: Settings) -> GraphClients:
        """Build the bundle from explicit settings, for one-shot operator entrypoints."""
        llm = LLM.from_settings(config)
        return cls(
            extractor=Extractor.configured(config, llm, GLiNER.from_settings(config)),
            gate=GateClient.from_settings(config),
            embed=EmbedClient.from_settings(config),
            llm=llm,
        )


def is_transient_db_error(error: BaseException) -> bool:
    """Whether a database error is a transient deadlock or serialization failure worth
    retrying."""
    if not isinstance(error, DBAPIError):
        return False
    return isinstance(getattr(error.orig, "orig", None), TransactionRollbackError)


@functools.cache
def extraction_semaphore() -> asyncio.Semaphore:
    """The process-wide cap on chunks extracting and consolidating at once."""
    return asyncio.Semaphore(settings.graph_build_concurrency)


async def pending_chunks(
    scopes: Scopes,
    limit: int | None,
    source: str | None,
    document_id: UUID7 | None = None,
) -> list[Chunk]:
    """List unprocessed chunks in one exact scope set, in deterministic order."""
    key = frozenset(scopes)
    selection = (
        select(Chunk)
        .where(Chunk.processed_at.is_(None))
        .where(Chunk.scopes == sorted(key))
        .order_by(Chunk.id)
        .limit(limit)
    )
    if source is not None:
        titled = select(Document.id).where(Document.title.ilike(f"%{source}%"))
        selection = selection.where(Chunk.document_id.in_(titled))
    if document_id is not None:
        selection = selection.where(Chunk.document_id == document_id)
    return list(await User.system(key).exec[Chunk](selection))


async def mark_processed(session: Session, chunk_id: UUID7) -> None:
    """Stamp one chunk's processed_at so pending_chunks never offers it again."""
    await session.exec(update(Chunk).where(Chunk.id == chunk_id).values(processed_at=func.now()))


async def graph_counts(scopes: Scopes) -> tuple[int, int]:
    """Return entity and fact claim counts in one exact scope set."""
    key = frozenset(scopes)
    async with User.system(key) as session:
        entities = (
            select(Entity.Claim.id.count())
            .where(Entity.Claim.scopes == sorted(key))
            .scalar_subquery()
        )
        # The count spans the whole claim history including superseded versions.
        facts = (
            select(Fact.Claim.id.count()).where(Fact.Claim.scopes == sorted(key)).scalar_subquery()
        )
        counts = (
            await session.exec(
                select(entities, facts).execution_options(**{settings.skip_live_gate: True})
            )
        ).one()
    return counts[0], counts[1]


def source_extraction(
    chunk: Chunk, document: Document | None
) -> tuple[list[ExtractedEntity], list[TimedFact]]:
    """Project explicit ontology declarations and dated journal entries."""
    if document is None or not document.title:
        return [], []
    journals = journal_facts(chunk.text, document.title)
    entities = (
        [
            ExtractedEntity(
                name=document.title,
                type=document.subject_type or System.Entity.CONCEPT,
            )
        ]
        if journals
        else []
    )
    if chunk.ord != 0:
        return entities, journals
    declaration = SourceDeclaration.from_text(chunk.text, document.title)
    if document.subject_type is not None:
        declaration = declaration.model_copy(update={"subject_type": document.subject_type})
    extracted = declaration.extraction(
        Ontology.current(),
        document.observed_at or document.created_at,
        document.expires_at,
    )
    return [*extracted.entities, *entities], [*extracted.facts, *journals]


async def model_extraction(
    chunk: Chunk, document: Document | None, clients: GraphClients
) -> tuple[list[ExtractedEntity], list[TimedFact]]:
    """Extract entities and dated facts, or return empty output for an irrelevant chunk."""
    if clients.extractor.requires_gate:
        with span("gate"):
            gate_relevant = await clients.gate.relevant(chunk.text)
        if not gate_relevant:
            logger.info("chunk {} gated out, no ontology-relevant entities", chunk.id)
            return [], []
    capture = CaptureContext.model_validate(chunk.provenance)
    with span("extract"):
        extraction = await clients.extractor.extract(capture.search_text(chunk.text))
    grounded = GroundedProjection.from_extraction(extraction, chunk.text)
    logger.bind(
        chunk_id=str(chunk.id),
        projection_quality=grounded.quality.model_dump(mode="json"),
    ).info(
        "projection quality accepted_facts={}/{} accepted_entities={}/{} rejected={}",
        grounded.quality.accepted_facts,
        grounded.quality.proposed_facts,
        grounded.quality.accepted_entities,
        grounded.quality.proposed_entities,
        grounded.quality.rejected_facts,
    )
    fallback = capture.observed_at or (
        document.created_at if document is not None else datetime.now(UTC)
    )
    return grounded.entities, with_source_fallback(grounded.facts, fallback, capture.expires_at)


async def prepare_entities(
    session: Session, entities: list[ExtractedEntity], embed: Embedder
) -> list[PreparedEntity]:
    """Resolve suggested types and embed entity names in one deduplicated model call."""
    entities = merge_entities(entities)
    suggestions = _suggested_types(entities)
    names = list(dict.fromkeys(entity.name for entity in entities))
    texts = list(dict.fromkeys([*suggestions, *names]))
    vectors = await embed.embed(texts, mode="document") if texts else []
    embedded = dict(zip(texts, vectors, strict=True))
    resolved_types: dict[str, str] = {}
    if suggestions:
        async with session.begin():
            resolved_types = await Ontology.current().resolve_entity_types(
                session,
                [(suggestion, embedded[suggestion]) for suggestion in suggestions],
            )
    return _prepared_entities(entities, embedded, resolved_types)


def _suggested_types(entities: list[ExtractedEntity]) -> list[str]:
    return list(
        dict.fromkeys(
            entity.suggested_type
            for entity in entities
            if entity.type == System.Entity.CONCEPT and entity.suggested_type is not None
        )
    )


def _prepared_entities(
    entities: list[ExtractedEntity],
    embedded: dict[str, list[float]],
    resolved_types: dict[str, str],
) -> list[PreparedEntity]:
    return [
        PreparedEntity(
            name=entity.name,
            type=resolved_types.get(entity.suggested_type or "", entity.type),
            vector=tuple(embedded[entity.name]),
        )
        for entity in entities
    ]


def merge_entities(entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
    """Keep one endpoint per name, preferring explicit or otherwise specific types."""
    merged: dict[str, ExtractedEntity] = {}
    for entity in entities:
        key = " ".join(entity.name.split()).casefold()
        standing = merged.get(key)
        if standing is None or (
            standing.type == System.Entity.CONCEPT and entity.type != System.Entity.CONCEPT
        ):
            merged[key] = entity
    return list(merged.values())


async def resolve_entities(
    writer: GraphWriter, entities: list[PreparedEntity]
) -> dict[str, UUID5]:
    """Resolve every extracted entity through the writer, name to resolved content id."""
    return await writer.resolve_all(entities)


def _transient_retries() -> AsyncRetrying:
    return AsyncRetrying(
        retry=retry_if_exception(is_transient_db_error),
        stop=stop_after_attempt(4),
        wait=wait_random_exponential(multiplier=0.05, max=1.0),
        reraise=True,
    )


async def _resolve_candidates(
    session: Session,
    writer: GraphWriter,
    prepared: list[PreparedEntity],
    facts: list[TimedFact],
) -> tuple[dict[str, UUID5], list[FactCandidate]]:
    resolved: dict[str, UUID5] = {}
    candidates: list[FactCandidate] = []
    async for attempt in _transient_retries():
        with attempt, span("resolve_entities"):
            async with session.begin():
                resolved = await resolve_entities(writer, prepared)
                candidates = await writer.new_candidates(facts, resolved)
    return resolved, candidates


async def _apply_plans(
    session: Session,
    writer: GraphWriter,
    chunk_id: UUID7,
    candidates: list[FactCandidate],
    vectors: list[list[float]],
    plans: list[FactPlan],
) -> tuple[bool, list[FactPlan]]:
    decisions = await writer.resolve_ambiguous(plans) if writer.borderline(plans) else []
    current: list[FactPlan] = []
    async for attempt in _transient_retries():
        with attempt, span("db_write"):
            async with session.begin():
                await writer.lock_plans(plans)
                current = await writer.plan_facts(candidates, vectors)
                if [plan.matches for plan in current] != [plan.matches for plan in plans]:
                    continue
                await writer.apply_plans(plans, decisions, chunk_id)
                await mark_processed(session, chunk_id)
                return True, current
    return False, current


async def _consolidate(
    session: Session,
    writer: GraphWriter,
    chunk_id: UUID7,
    candidates: list[FactCandidate],
    vectors: list[list[float]],
    plans: list[FactPlan],
) -> None:
    for _ in range(4):
        applied, plans = await _apply_plans(session, writer, chunk_id, candidates, vectors, plans)
        if applied:
            return
    raise RuntimeError(f"graph slice {chunk_id} changed during four consolidation attempts")


async def write_graph_slice(
    opened: Session,
    chunk: Chunk,
    entities: list[ExtractedEntity],
    dated_facts: list[TimedFact],
    clients: GraphClients,
) -> set[UUID5]:
    """Plan model work between short entity, read, and final write transactions."""
    capture = CaptureContext.model_validate(chunk.provenance)
    writer = GraphWriter(
        session=opened,
        created_by=chunk.created_by,
        scopes=frozenset(chunk.scopes),
        consolidator=Consolidator(llm=clients.llm),
        capture=capture,
        source_text=chunk.text,
    )
    async with opened.begin():
        entities, dated_facts = await Ontology.normalize(opened, entities, dated_facts)
    prepared = await prepare_entities(opened, entities, clients.embed)
    resolved, candidates = await _resolve_candidates(opened, writer, prepared, dated_facts)
    vectors = (
        await clients.embed.embed(
            [candidate.fact.statement for candidate in candidates], mode="document"
        )
        if candidates
        else []
    )
    async with opened.begin():
        plans = await writer.plan_facts(candidates, vectors)
    await _consolidate(opened, writer, chunk.id, candidates, vectors, plans)
    return set(resolved.values())


async def extract_and_consolidate(chunk: Chunk, clients: GraphClients) -> set[UUID5]:
    """Extract, resolve, and consolidate one chunk's graph slice, return the entities it
    touched."""
    key = frozenset(chunk.scopes)
    async with extraction_semaphore(), User.system(key).session() as opened:
        async with opened.begin():
            document = await opened.get(Document, chunk.document_id)
            entities, dated_facts = source_extraction(chunk, document)
        short = len(chunk.text.strip()) < settings.extract_min_chars
        if short and not entities and not dated_facts:
            async with opened.begin():
                await mark_processed(opened, chunk.id)
            return set()
        if not short:
            extracted_entities, extracted_facts = await model_extraction(chunk, document, clients)
            entities = [*entities, *extracted_entities]
            dated_facts = [*dated_facts, *extracted_facts]
        touched = await write_graph_slice(opened, chunk, entities, dated_facts, clients)
        logger.info("graph slice from chunk {} done", chunk.id)
        return touched


def raise_failures(chunks: list[Chunk], results: list[set[UUID5] | BaseException]) -> None:
    """Raise chunk failures after every independent concurrent write has had a chance to
    finish."""
    failures: list[BaseException] = []
    for chunk, result in zip(chunks, results, strict=True):
        if isinstance(result, BaseException):
            logger.error("chunk {} failed unexpectedly: {}", chunk.id, result)
            failures.append(result)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("multiple graph chunks failed", failures)


async def build_graph(
    clients: GraphClients,
    limit: int | None = None,
    scopes: Scopes | None = None,
    source: str | None = None,
) -> tuple[int, int]:
    """Build the graph from chunks the build has never run over and return the counts
    created."""
    key = frozenset(scopes or (settings.system_user_id,))
    async with User.system(key) as session:
        await Ontology.ensure(session)
    chunks = await pending_chunks(key, limit, source)
    entities_before, facts_before = await graph_counts(key)
    results = await asyncio.gather(
        *(extract_and_consolidate(chunk, clients) for chunk in chunks),
        return_exceptions=True,
    )
    raise_failures(chunks, results)
    entities_after, facts_after = await graph_counts(key)
    return entities_after - entities_before, facts_after - facts_before
