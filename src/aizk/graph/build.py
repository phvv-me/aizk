import asyncio
import functools
import uuid
from datetime import UTC, datetime

from asyncpg.exceptions import TransactionRollbackError
from loguru import logger
from mainboard.profiling import span
from openai import APIConnectionError, APITimeoutError, LengthFinishReasonError
from pydantic import ValidationError
from sqlalchemy import func, update
from sqlalchemy.exc import DBAPIError
from sqlmodel import select
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from ..config import settings
from ..exceptions import ExtractionUnreachableError
from ..extract import journal, ontology
from ..extract.dating import with_document_fallback
from ..extract.llm import decide_consolidations_batch
from ..extract.models import ExtractedEntity, TimedFact
from ..extract.strategies import extract_graph
from ..provenance import CaptureContext
from ..serving import embed, relevant
from ..store import (
    Chunk,
    Document,
    EntityClaim,
    FactClaim,
)
from ..store.engine import Session, session_for
from ..store.identity import User
from ..types import Scopes
from .consolidation import cosine_similarity
from .writer import FactCandidate, FactPlan, GraphWriter, PreparedEntity


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


class ChunkExtractionTimedOut(Exception):
    """Internal signal that one chunk's extraction call exceeded extract_timeout."""


async def pending_chunks(scopes: Scopes, limit: int | None, source: str | None) -> list[Chunk]:
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
    async with User.system(key) as session:
        return list(await session.exec(selection))


async def mark_processed(session: Session, chunk_id: uuid.UUID) -> None:
    """Stamp one chunk's processed_at so pending_chunks never offers it again."""
    await session.exec(update(Chunk).where(Chunk.id == chunk_id).values(processed_at=func.now()))


async def graph_counts(scopes: Scopes) -> tuple[int, int]:
    """Return entity and fact claim counts in one exact scope set."""
    key = frozenset(scopes)
    async with User.system(key) as session:
        entities = (
            select(func.count())
            .select_from(EntityClaim)
            .where(EntityClaim.scopes == sorted(key))
            .scalar_subquery()
        )
        # The count spans the whole claim history including superseded versions.
        facts = (
            select(func.count())
            .select_from(FactClaim)
            .where(FactClaim.scopes == sorted(key))
            .scalar_subquery()
        )
        counts = (
            await session.exec(
                select(entities, facts).execution_options(**{settings.skip_live_gate: True})
            )
        ).one()
    return counts[0], counts[1]


async def document_declared_type(session: Session, document_id: uuid.UUID) -> str | None:
    """The structural type any sibling chunk of a document declares, Area or Project, else
    None."""
    siblings = await session.exec(select(Chunk.text).where(Chunk.document_id == document_id))
    for text in siblings:
        declared = journal.declared_type(text)
        if declared is not None:
            return declared
    return None


async def journal_extraction(
    session: Session, chunk: Chunk, document: Document | None
) -> tuple[list[ExtractedEntity], list[TimedFact]]:
    """The note's declared title entity and any dated journal facts, empty when neither
    applies."""
    if document is None or not document.title:
        return [], []
    declared = journal.declared_type(chunk.text)
    has_journal = journal.has_journal_entries(chunk.text)
    if declared is None and not has_journal:
        return [], []
    if declared is None:
        declared = await document_declared_type(session, chunk.document_id)
    entity = journal.title_entity(document.title, declared)
    facts = journal.journal_facts(chunk.text, document.title) if has_journal else []
    return [entity], facts


async def llm_extraction(
    chunk: Chunk, document: Document | None
) -> tuple[list[ExtractedEntity], list[TimedFact]]:
    """The combined-call entities and dated facts, empty when the chunk gates out or
    truncates."""
    with span("gate"):
        gate_relevant = await relevant(chunk.text)
    if not gate_relevant:
        logger.info("chunk {} gated out, no ontology-relevant entities", chunk.id)
        return [], []
    try:
        with span("extract"):
            capture = CaptureContext.model_validate(chunk.provenance)
            extraction = await extract_graph(capture.search_text(chunk.text))
    except APITimeoutError as error:
        logger.warning("extraction timed out on chunk {}, skipping", chunk.id)
        raise ChunkExtractionTimedOut from error
    except APIConnectionError as error:
        raise ExtractionUnreachableError(
            f"cannot reach the extraction endpoint at {settings.llm_url!r} ({error}); "
            "confirm AIZK_LLM_URL points at a running server (the vllm-llm compose "
            "service or a cloud provider)"
        ) from error
    except LengthFinishReasonError, ValidationError:
        # A deterministic token-limit failure is terminal until the configured limit changes.
        logger.warning(
            "chunk {} extraction exceeded extract_max_tokens={}, skipping; raise "
            "AIZK_EXTRACT_MAX_TOKENS and rerun the build to recover it",
            chunk.id,
            settings.extract_max_tokens,
        )
        return [], []
    capture = CaptureContext.model_validate(chunk.provenance)
    fallback = capture.observed_at or (
        document.created_at if document is not None else datetime.now(UTC)
    )
    return extraction.entities, with_document_fallback(extraction.facts, fallback)


def closest_entity_type(vector: list[float]) -> str:
    """Map one suggested-type embedding onto the curated ontology."""
    scored = [
        (name, cosine_similarity(vector, candidate))
        for name, candidate in ontology.current().entity_description_vectors.items()
    ]
    if not scored:
        return ontology.CONCEPT
    name, similarity = max(scored, key=lambda pair: pair[1])
    return name if similarity >= settings.ontology_match_threshold else ontology.CONCEPT


async def prepare_entities(entities: list[ExtractedEntity]) -> list[PreparedEntity]:
    """Resolve suggested types and embed entity names in one deduplicated model call."""
    suggestions = list(
        dict.fromkeys(
            entity.suggested_type
            for entity in entities
            if entity.type == ontology.CONCEPT and entity.suggested_type is not None
        )
    )
    names = list(dict.fromkeys(entity.name for entity in entities))
    texts = list(dict.fromkeys([*suggestions, *names]))
    embedded = dict(zip(texts, await embed(texts, mode="document") if texts else [], strict=True))
    resolved_types = {
        suggestion: closest_entity_type(embedded[suggestion]) for suggestion in suggestions
    }
    return [
        PreparedEntity(
            name=entity.name,
            type=resolved_types.get(entity.suggested_type or "", entity.type),
            vector=tuple(embedded[entity.name]),
        )
        for entity in entities
    ]


async def resolve_entities(
    writer: GraphWriter, entities: list[PreparedEntity]
) -> dict[str, uuid.UUID]:
    """Resolve every extracted entity through the writer, name to resolved content id."""
    return await writer.resolve_all(entities)


async def write_graph_slice(
    opened: Session,
    chunk: Chunk,
    entities: list[ExtractedEntity],
    dated_facts: list[TimedFact],
) -> set[uuid.UUID]:
    """Plan model work between short entity, read, and final write transactions."""
    capture = CaptureContext.model_validate(chunk.provenance)
    writer = GraphWriter(opened, chunk.created_by, frozenset(chunk.scopes), capture, chunk.text)
    prepared = await prepare_entities(entities)
    resolved: dict[str, uuid.UUID] = {}
    candidates: list[FactCandidate] = []
    async for attempt in AsyncRetrying(
        retry=retry_if_exception(is_transient_db_error),
        stop=stop_after_attempt(4),
        wait=wait_random_exponential(multiplier=0.05, max=1.0),
        reraise=True,
    ):
        with attempt, span("resolve_entities"):
            async with opened.begin():
                resolved = await resolve_entities(writer, prepared)
                candidates = await writer.new_candidates(dated_facts, resolved)
    vectors = (
        await embed([candidate.fact.statement for candidate in candidates], mode="document")
        if candidates
        else []
    )
    async with opened.begin():
        plans = await writer.plan_facts(candidates, vectors)
    for _ in range(4):
        borderline = writer.borderline(plans)
        decisions = await decide_consolidations_batch(borderline) if borderline else []
        current: list[FactPlan] = []
        applied = False
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(is_transient_db_error),
            stop=stop_after_attempt(4),
            wait=wait_random_exponential(multiplier=0.05, max=1.0),
            reraise=True,
        ):
            with attempt, span("db_write"):
                async with opened.begin():
                    await writer.lock_plans(plans)
                    current = await writer.plan_facts(candidates, vectors)
                    if [plan.matches for plan in current] != [plan.matches for plan in plans]:
                        continue
                    await writer.apply_plans(plans, decisions, chunk.id)
                    await mark_processed(opened, chunk.id)
                    applied = True
        if applied:
            return set(resolved.values())
        plans = current
    raise RuntimeError(f"graph slice {chunk.id} changed during four consolidation attempts")


async def extract_and_consolidate(chunk: Chunk) -> set[uuid.UUID]:
    """Extract, resolve, and consolidate one chunk's graph slice, return the entities it
    touched."""
    key = frozenset(chunk.scopes)
    async with extraction_semaphore(), session_for(User.system(key)) as opened:
        async with opened.begin():
            document = await opened.get(Document, chunk.document_id)
            entities, dated_facts = await journal_extraction(opened, chunk, document)
        short = len(chunk.text.strip()) < settings.extract_min_chars
        if short and not dated_facts:
            async with opened.begin():
                await mark_processed(opened, chunk.id)
            return set()
        if not short:
            try:
                llm_entities, llm_facts = await llm_extraction(chunk, document)
            except ChunkExtractionTimedOut:
                return set()
            entities = [*entities, *llm_entities]
            dated_facts = [*dated_facts, *llm_facts]
        touched = await write_graph_slice(opened, chunk, entities, dated_facts)
        logger.info("graph slice from chunk {} done", chunk.id)
        return touched


def raise_failures(chunks: list[Chunk], results: list[set[uuid.UUID] | BaseException]) -> None:
    """Raise chunk failures after every independent concurrent write has had a chance to
    finish."""
    failures: list[BaseException] = []
    for chunk, result in zip(chunks, results, strict=True):
        if isinstance(result, ExtractionUnreachableError):
            raise result
        if isinstance(result, BaseException):
            logger.error("chunk {} failed unexpectedly: {}", chunk.id, result)
            failures.append(result)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("multiple graph chunks failed", failures)


async def build_graph(
    limit: int | None = None,
    scopes: Scopes | None = None,
    source: str | None = None,
) -> tuple[int, int]:
    """Build the graph from chunks the build has never run over and return the counts
    created."""
    key = frozenset(scopes or (settings.system_user_id,))
    async with User.system(key) as session:
        await ontology.ensure_current(session)
    chunks = await pending_chunks(key, limit, source)
    entities_before, facts_before = await graph_counts(key)
    results = await asyncio.gather(
        *(extract_and_consolidate(chunk) for chunk in chunks),
        return_exceptions=True,
    )
    raise_failures(chunks, results)
    entities_after, facts_after = await graph_counts(key)
    return entities_after - entities_before, facts_after - facts_before
