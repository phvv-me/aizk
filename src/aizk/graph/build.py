import asyncio
import functools
import uuid
from collections import defaultdict
from datetime import UTC, datetime

from asyncpg.exceptions import TransactionRollbackError
from loguru import logger
from mainboard.profiling import span
from openai import APIConnectionError, APITimeoutError, LengthFinishReasonError
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from ..config import settings
from ..exceptions import ExtractionUnreachableError
from ..extract import journal, ontology
from ..extract.dating import with_document_fallback
from ..extract.llm import combined_extract, decide_consolidations_batch
from ..extract.models import ConsolidationVerdict, ExtractedEntity, TimedFact
from ..serving import Embedder, EntityGate
from ..store import (
    Chunk,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    LiveFact,
    Membership,
    acting_as,
)
from ..store.engine import bypass_rls, session
from .consolidation import decide_by_rule, rank_pool
from .dedupe import claim_entity, claim_fact, mint_content
from .ids import entity_id, fact_id
from .naming import normalize_name
from .ontology_growth import resolve_suggested_type


def is_transient_db_error(error: BaseException) -> bool:
    """Whether a database error is a transient deadlock or serialization failure worth retrying.

    Postgres detects and aborts one side of a deadlock or a serializable-isolation conflict rather
    than blocking forever, and asyncpg surfaces both as a `TransactionRollbackError` subclass. At
    the graph's own concurrency (`settings.graph_build_concurrency`, dozens of chunks minting and
    claiming entity content at once), two chunks racing to mint the same entity is exactly the
    shape of contention that trips this, so `write_graph_slice`'s retry loop retries on it rather
    than losing a chunk to noise. SQLAlchemy's asyncpg dialect has no explicit mapping for either
    error, so it arrives wrapped twice, `DBAPIError.orig` holds the dialect's own emulated DBAPI
    exception, and that exception's own `.orig` holds the real asyncpg error this checks.

    error: the exception `write_graph_slice`'s retry loop caught.
    """
    if not isinstance(error, DBAPIError):
        return False
    return isinstance(getattr(error.orig, "orig", None), TransactionRollbackError)


@functools.cache
def extraction_semaphore() -> asyncio.Semaphore:
    """The process-wide cap on chunks extracting and consolidating at once.

    Shared by `build_graph`'s inline concurrent loop and the pgqueuer worker's extraction
    entrypoint (`background.queue.process_chunk`), so both paths bound how hard they hit the LLM
    endpoint at the same `settings.graph_build_concurrency` width regardless of how many chunk
    jobs pgqueuer itself has dispatched as concurrent asyncio tasks. Cached the way
    `store.engine.app_sessions` caches its engine, one semaphore for the process's lifetime.
    """
    return asyncio.Semaphore(settings.graph_build_concurrency)


class ChunkExtractionTimedOut(Exception):
    """Internal signal that one chunk's extraction call exceeded extract_timeout.

    Raised by `llm_extraction` and caught by `extract_and_consolidate`, which abandons the chunk
    outright rather than writing a partial graph slice, leaving it pending for a later retry.
    """


class GraphWriter:
    """One graph-write round bound to the session, owner, and scope set every write in it shares.

    resolve and consolidate_facts both re-thread (owner_id, scopes) through every call, so binding
    them once here turns each repeated argument list into a `self` read (the session itself now
    comes from the task-local context via `session()`). write_graph_slice
    opens one GraphWriter per chunk on its owner-scoped transaction, the shared core both
    build_graph's concurrent loop and background.queue.process_chunk's durable job call.

    Every mint below is an idempotent `INSERT ... ON CONFLICT DO NOTHING`, on content's own id for
    the deduplicated structural row and on the claim table's own uniqueness for this container's
    stake (a container is the owner-plus-scopes tenant a claim belongs to), so two owners
    independently extracting the identical entity or fact land the exact same statements whichever
    one runs first. Content is minted once and shared, each owner's own claim rides beside it, and
    neither a primary-key collision nor a success/failure timing difference ever tells one owner
    whether the other's private content already existed.

    owner_id: user that owns a newly created claim.
    scopes: group set a newly created claim is shared with, private when empty. Always the
        already-canonicalized (sorted) tuple a chunk's own `scopes` carries, so every claim this
        writer mints compares equal to any other write of the identical set.
    """

    def __init__(self, owner_id: uuid.UUID, scopes: tuple[uuid.UUID, ...]) -> None:
        self.owner_id = owner_id
        self.scopes = list(scopes)

    async def already_claims_entity(self, content_id: uuid.UUID) -> bool:
        """Whether this container already stakes its own claim on this entity content id."""
        claimed = await session().scalar(
            select(EntityClaim.id).where(
                EntityClaim.content_id == content_id,
                EntityClaim.owner_id == self.owner_id,
                EntityClaim.scopes == self.scopes,
            )
        )
        return claimed is not None

    async def match_or_mint_entity(self, name: str, type: str, node: uuid.UUID) -> uuid.UUID:
        """The best cosine match already visible to this container, or a freshly minted content id.

        name: entity surface form, already known non-empty.
        type: ontology entity type the match must share.
        node: content-addressed id a fresh mint uses, from `graph.ids.entity_id`.
        """
        with span("embed"):
            [vector] = await Embedder().embed([name], mode="document")
        distance = EntityContent.embedding.cosine_distance(vector)
        match = await session().scalar(
            select(EntityContent.id)
            .where(EntityContent.type == type)
            .where(distance <= 1.0 - settings.entity_resolution_threshold)
            .order_by(distance)
            .limit(1)
        )
        if match is not None:
            return match
        await mint_content(EntityContent(id=node, name=name, type=type, embedding=vector))
        return node

    async def resolve(self, name: str, type: str) -> uuid.UUID | None:
        """Resolve a name to a stored content id, always minting this container's own claim on it.

        Normalizes the name first so a path or url echoed as an entity folds to empty and is
        dropped with a null return. When this container already claims the exact content-addressed
        id, that fast path returns immediately with no embedding call. Otherwise
        `match_or_mint_entity` embeds the cleaned name and cosine matches the entity content
        already visible to this container (through one of its own or a shared claim) within the
        same type. A match above settings.entity_resolution_threshold still mints this container's
        own claim on that content before returning its id, since visibility through another
        tenant's claim is not this container's own stake, and consolidate_facts resolves every
        fact's subject and object through this same map, so a claim that never lands here is a
        fact this container can never write either. With no match it mints a fresh content row.

        name: entity surface form to resolve.
        type: ontology entity type the match must share.
        """
        if not normalize_name(name):
            logger.warning("entity name {!r} is a path or link, dropping", name)
            return None
        node = entity_id(name, type)
        if await self.already_claims_entity(node):
            return node
        resolved = await self.match_or_mint_entity(name, type, node)
        await claim_entity(resolved, self.owner_id, self.scopes)
        return resolved

    async def already_claims_fact(self, content_id: uuid.UUID) -> bool:
        """Whether this container already stakes a live claim on this fact content id right now.

        Gated to the current version through the ORM's own live temporal criteria, so a claim
        this container once held but has since superseded, decayed, or forgotten no longer reads
        as claimed. That lets a later chunk re-assert a byte-identical statement the world has
        reverted to, opening a fresh live claim, rather than silently dropping it against a closed
        one that could never be re-opened.
        """
        claimed = await session().scalar(
            select(FactClaim.id).where(
                FactClaim.content_id == content_id,
                FactClaim.owner_id == self.owner_id,
                FactClaim.scopes == self.scopes,
            )
        )
        return claimed is not None

    async def candidate(
        self, fact: TimedFact, resolved: dict[str, uuid.UUID]
    ) -> tuple[TimedFact, uuid.UUID, uuid.UUID | None, uuid.UUID] | None:
        """One fact's live candidate tuple, or null when already claimed or its subject never
        resolved.

        fact: an extracted, dated candidate fact from one chunk.
        resolved: entity surface name to resolved content id, from this chunk's own `resolve`
            calls.
        """
        identity = fact_id(fact.subject, fact.predicate, fact.object_, fact.statement)
        if await self.already_claims_fact(identity):
            return None
        subject_id = resolved.get(fact.subject)
        if subject_id is None:
            logger.warning("fact subject {!r} has no resolved entity, skipping", fact.subject)
            return None
        object_id = resolved.get(fact.object_) if fact.object_ else None
        return fact, subject_id, object_id, identity

    async def new_candidates(
        self, facts: list[TimedFact], resolved: dict[str, uuid.UUID]
    ) -> list[tuple[TimedFact, uuid.UUID, uuid.UUID | None, uuid.UUID]]:
        """The facts not already claimed by this container and whose subject resolved to a real
        entity, the consolidation cascade's first, free tier.

        facts: the extracted, dated candidate facts from one chunk.
        resolved: entity surface name to resolved content id, from this chunk's own `resolve`
            calls.
        """
        candidates = [await self.candidate(fact, resolved) for fact in facts]
        return [candidate for candidate in candidates if candidate is not None]

    async def live_facts_by_subject(
        self, subject_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, list[LiveFact]]:
        """Every visible latest claim for a set of subjects, one query for a whole chunk's batch.

        The non-LLM consolidation cascade's batched read. Rather than one `ORDER BY <vector>`
        query per candidate fact, this fetches the unordered pool for every distinct subject a
        chunk's candidates name at once, and `graph.consolidation.rank_pool` then ranks each
        candidate's own slice of the pool by cosine similarity in Python, since a single SQL
        statement cannot `ORDER BY` a different query vector per row.

        subject_ids: the distinct resolved subject content ids this chunk's candidates name.
        """
        if not subject_ids:
            return {}
        pools: dict[uuid.UUID, list[LiveFact]] = defaultdict(list)
        for claim in await session().scalars(
            select(LiveFact).where(LiveFact.subject_id.in_(subject_ids))
        ):
            pools[claim.subject_id].append(claim)
        return pools

    async def consolidate_facts(
        self,
        facts: list[TimedFact],
        resolved: dict[str, uuid.UUID],
        source_chunk_id: uuid.UUID,
    ) -> None:
        """Consolidate every dated fact from one chunk through the non-LLM cascade.

        Embeds every surviving statement in one batched call, ranks each against its subject's
        live-fact pool (`live_facts_by_subject`, also fetched once for the whole chunk), and
        decides every candidate whose top match is unambiguous by rule alone
        (`graph.consolidation.decide_by_rule`). Whatever is left, the genuinely borderline
        candidates whose top match falls in the ambiguous cosine band, resolves in one further
        batched LLM call (`decide_consolidations_batch`) rather than one per fact, so a chunk
        never pays more than two LLM calls total, the combined extraction call and this one.

        facts: the extracted, dated candidate facts from one chunk.
        resolved: entity surface name to resolved content id, from this chunk's own `resolve`
            calls.
        source_chunk_id: chunk the facts were extracted from, stamped as provenance on new claims.
        """
        candidates = await self.new_candidates(facts, resolved)
        if not candidates:
            return
        with span("consolidate"):
            with span("embed"):
                vectors = await Embedder().embed(
                    [fact.statement for fact, _, _, _ in candidates], mode="document"
                )
            pools = await self.live_facts_by_subject(
                {subject_id for _, subject_id, _, _ in candidates}
            )
            scored = [
                rank_pool(vector, pools.get(subject_id, []))
                for (_, subject_id, _, _), vector in zip(candidates, vectors, strict=True)
            ]
            verdicts: list[ConsolidationVerdict | None] = [
                decide_by_rule(fact.predicate, object_id, pool)
                for (fact, _, object_id, _), pool in zip(candidates, scored, strict=True)
            ]
            borderline = [
                (fact, [claim for claim, _ in pool])
                for (fact, _, _, _), pool, verdict in zip(
                    candidates, scored, verdicts, strict=True
                )
                if verdict is None
            ]
            if borderline:
                verdicts = merged_verdicts(verdicts, await decide_consolidations_batch(borderline))
            for (fact, subject_id, object_id, identity), vector, verdict in zip(
                candidates, vectors, verdicts, strict=True
            ):
                assert verdict is not None  # every slot above is filled, directly or by the batch
                await self.apply_verdict(
                    fact, subject_id, object_id, identity, vector, source_chunk_id, verdict
                )

    async def close_superseded_claim(
        self, supersedes: uuid.UUID, valid_from: datetime | None, now: datetime
    ) -> None:
        """Close the claim an UPDATE verdict supersedes, clamping its valid range non-inverted.

        The superseding fact's own valid_from is the natural close point, but a backdated
        correction can name a start earlier than the retired claim's own. Clamping to its lower
        bound keeps the range non-inverted (an immediately-closed window) rather than raising a
        range-order violation the database itself would refuse.

        supersedes: id of the claim to close.
        valid_from: the new fact's own valid-time start, the natural close point.
        now: this verdict's own write time, closing `recorded` and defaulting `valid_from`.
        """
        gate_off = {settings.skip_live_gate: True}
        retired = await session().get(FactClaim, supersedes, execution_options=gate_off)
        if retired is None:
            return
        lower = retired.valid.lower if retired.valid else None
        closing = valid_from or now
        if lower is not None and closing < lower:
            closing = lower
        retired.valid = Range(lower, closing)
        retired.recorded = Range(retired.recorded.lower, now)

    async def apply_verdict(
        self,
        fact: TimedFact,
        subject_id: uuid.UUID,
        object_id: uuid.UUID | None,
        identity: uuid.UUID,
        vector: list[float],
        source_chunk_id: uuid.UUID,
        verdict: ConsolidationVerdict,
    ) -> None:
        """Apply one fact's already-decided ADD, UPDATE, or NOOP verdict to this container's canon.

        On UPDATE closes the old claim before upserting the new one, on ADD upserts the new
        content and claim, and on NOOP leaves the graph unchanged. This chunk's only per-fact
        write, once `consolidate_facts` has already resolved subjects, embedded statements, and
        decided every verdict together.

        # DEFER HippoRAG Personalized PageRank multi-hop retrieval attaches here, walking the
        # edges this method writes to rank facts beyond first-order similarity.

        fact: the extracted candidate fact, already known not yet claimed by this container.
        subject_id: resolved entity content id the fact is about.
        object_id: resolved entity content id the fact points to, null for a unary fact.
        identity: content-addressed id for this fact's triple and statement.
        vector: the statement's already-embedded dense vector.
        source_chunk_id: chunk the fact was extracted from, stamped as provenance on the new claim.
        verdict: the already-decided ADD, UPDATE, or NOOP action, from the non-LLM cascade or the
            batched borderline call.
        """
        if verdict.action == "NOOP":
            return
        now = datetime.now(UTC)
        if verdict.action == "UPDATE" and verdict.supersedes is not None:
            await self.close_superseded_claim(verdict.supersedes, fact.valid_from, now)
        await mint_content(
            FactContent(
                id=identity,
                subject_id=subject_id,
                object_id=object_id,
                predicate=fact.predicate,
                statement=fact.statement,
                embedding=vector,
            ),
        )
        await claim_fact(
            identity,
            self.owner_id,
            self.scopes,
            valid=Range(fact.valid_from, fact.valid_to)
            if fact.valid_from or fact.valid_to
            else None,
            source_chunk_id=source_chunk_id,
        )


def merged_verdicts(
    verdicts: list[ConsolidationVerdict | None], resolved: list[ConsolidationVerdict]
) -> list[ConsolidationVerdict | None]:
    """Fill each null, genuinely-ambiguous slot with the batched LLM's own verdict, in order.

    Every slot is non-null once filled (the caller's own `assert verdict is not None` documents
    it). The return type stays `| None` only so it matches `verdicts`' own declared type at the
    reassignment `consolidate_facts` makes, list element types being invariant.

    verdicts: the non-LLM cascade's own decisions, null wherever it deferred.
    resolved: the batched borderline call's verdicts, one per deferred slot, in order.
    """
    pending = iter(resolved)
    return [verdict if verdict is not None else next(pending) for verdict in verdicts]


async def pending_chunks(
    user_id: uuid.UUID,
    limit: int | None,
    source: str | None,
) -> list[Chunk]:
    """List the writable chunks the graph build has never run over, in a fixed deterministic order.

    A chunk counts as pending until its own `processed_at` is set, so a finished build never
    reprocesses it and a build resumed after an interruption picks up where it stopped, regardless
    of whether that earlier pass minted any claim at all. Extraction stamps its entities and facts
    with the source chunk's scope set, so only chunks in scope sets the user may write are
    pending for it. A reader or public visitor never extracts into someone else's shared graph,
    that scope's own writers do.

    user_id: identity whose row level security visibility and write access scope the chunks.
    limit: maximum number of chunks to return, all of them when null.
    source: when set, restrict to chunks of documents whose title matches this substring.
    """
    selection = (
        select(Chunk)
        .where(Chunk.processed_at.is_(None))
        .where(Membership.writable_scopes(Chunk.scopes, Chunk.owner_id, user_id))
        .order_by(Chunk.id)
        .limit(limit)
    )
    if source is not None:
        titled = select(Document.id).where(Document.title.ilike(f"%{source}%"))
        selection = selection.where(Chunk.document_id.in_(titled))
    async with acting_as(user_id):
        return list(await session().scalars(selection))


async def mark_processed(chunk_id: uuid.UUID) -> None:
    """Stamp one chunk's processed_at so pending_chunks never offers it again.

    Set unconditionally once extraction and consolidation have run over the chunk, whether or not
    that pass minted any entity or fact, so a chunk whose prose asserts nothing worth keeping is
    never re-extracted on the next build. Left unset entirely when extraction itself fails, the
    transient case a later build retries.

    chunk_id: chunk to mark processed.
    """
    await session().execute(
        update(Chunk).where(Chunk.id == chunk_id).values(processed_at=func.now())
    )


async def graph_counts(user_id: uuid.UUID) -> tuple[int, int]:
    """Return the entity and fact claim counts visible to a user under row level security.

    user_id: identity whose row level security visibility scopes the counts.
    """
    async with acting_as(user_id):
        entities = await session().scalar(select(func.count()).select_from(EntityClaim)) or 0
        # the count spans the whole visible claim history including superseded versions, so it
        # opts out of the live gate that would otherwise narrow it to the latest open claims.
        facts = (
            await session().scalar(
                select(func.count())
                .select_from(FactClaim)
                .execution_options(**{settings.skip_live_gate: True})
            )
            or 0
        )
    return entities, facts


async def document_declared_type(document_id: uuid.UUID) -> str | None:
    """The structural type any sibling chunk of a document declares, Area or Project, else None.

    Read only for a journal-line chunk that does not itself carry the declaring tag, since the tag
    usually lives in the front-matter chunk rather than the one carrying the dated line. Every
    sibling's text is scanned rather than only this chunk's own.

    document_id: the chunk's parent document.
    """
    siblings = await session().scalars(select(Chunk.text).where(Chunk.document_id == document_id))
    for text in siblings:
        declared = journal.declared_type(text)
        if declared is not None:
            return declared
    return None


async def journal_extraction(
    user_id: uuid.UUID, chunk: Chunk, document: Document | None
) -> tuple[list[ExtractedEntity], list[TimedFact]]:
    """The note's declared title entity and any dated journal facts, empty when neither applies.

    Two deterministic, no-LLM signals combine here, the trust-declared-structure path. A note that
    declares its own kind through a #project or #area tag contributes its title as that typed
    entity, so the projects and areas rosters are exactly the notes that named themselves rather
    than whatever the extractor over-tagged. And extract.journal's `- YYYY-MM-DD: text` lines
    contribute dated facts logged against that title. A chunk declaring nothing and carrying no
    dated line returns empty, left to the LLM to characterize, and one carrying either is never
    skipped by extract_min_chars for being short since the signal is already the whole fact.

    user_id: identity that will own the written claims, whose visibility scopes the sibling
        tag read.
    chunk: the chunk whose text is scanned for the declaring tag and the dated line.
    document: the chunk's parent document, null when it no longer exists.
    """
    if document is None or not document.title:
        return [], []
    declared = journal.declared_type(chunk.text)
    has_journal = bool(journal.JOURNAL_LINE.search(chunk.text))
    if declared is None and not has_journal:
        return [], []
    if declared is None:
        async with acting_as(user_id):
            declared = await document_declared_type(chunk.document_id)
    entity = journal.title_entity(document.title, declared)
    facts = journal.journal_facts(chunk.text, document.title) if has_journal else []
    return [entity], facts


async def llm_extraction(
    chunk: Chunk, document: Document | None
) -> tuple[list[ExtractedEntity], list[TimedFact]]:
    """The combined-call entities and dated facts, empty when the chunk gates out or truncates.

    A chunk first passes the GLiNER2 relevance gate (`serving.EntityGate`, a 205M CPU model
    scoring the chunk against the ontology's own entity types in milliseconds). A chunk naming
    none of them returns empty with no LLM call at all. A chunk that clears the gate runs the one
    combined extraction call (`extract.llm.combined_extract`).

    Raises ChunkExtractionTimedOut when the call exceeds extract_timeout, the caller's signal to
    abandon the chunk outright rather than write a partial slice. Raises
    ExtractionUnreachableError when the endpoint itself is unreachable, a systemic failure
    `build_graph` still surfaces immediately rather than skipping like a per-chunk one.

    chunk: the chunk to extract from, already known to clear extract_min_chars.
    document: the chunk's parent document, whose created_at seeds an undated fact's fallback.
    """
    gate_relevant = True
    if settings.gliner_gate_enabled:
        with span("gate"):
            gate_relevant = await asyncio.to_thread(EntityGate().relevant, chunk.text)
    if not gate_relevant:
        logger.info("chunk {} gated out, no ontology-relevant entities", chunk.id)
        return [], []
    try:
        with span("extract"):
            extraction = await combined_extract(chunk.text)
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
        # a chunk rich enough that its structural extraction can never finish inside
        # extract_max_tokens fails identically on every retry, so it is marked processed with
        # only its journal facts, if any, rather than left pending to loop forever; raising the
        # setting and rerunning the build is the deliberate fix, not an automatic one. The same
        # truncation surfaces two ways: the openai SDK raises LengthFinishReasonError when the
        # endpoint reports finish_reason "length", but under xgrammar-guided decoding vLLM does
        # not always report it that way, so a cut-off response instead reaches here as a raw
        # pydantic ValidationError (an unterminated JSON string) once `structured` tries to parse
        # it; both are the identical truncation, just caught at two different layers.
        logger.warning(
            "chunk {} extraction exceeded extract_max_tokens={}, skipping; raise "
            "AIZK_EXTRACT_MAX_TOKENS and rerun the build to recover it",
            chunk.id,
            settings.extract_max_tokens,
        )
        return [], []
    fallback = document.created_at if document is not None else datetime.now(UTC)
    return extraction.entities, with_document_fallback(extraction.facts, fallback)


async def resolve_entity_type(writer: GraphWriter, entity: ExtractedEntity) -> str:
    """The entity kind this entity should actually resolve against, growing the catalog when the
    extractor named `Concept` but offered a more specific guess.

    The wire schema's own grammar constrains `type` to the live catalog, so a genuinely new kind
    can only ever arrive through `suggested_type`, never through `type` itself, `graph.
    ontology_growth.resolve_suggested_type` is where that free-text guess becomes a real,
    reusable kind rather than an ignored hint.

    writer: this chunk's own GraphWriter, its session the auto-create cascade writes through.
    entity: the extracted entity whose type may still be growing the ontology.
    """
    if entity.type != ontology.CONCEPT or entity.suggested_type is None:
        return entity.type
    return await resolve_suggested_type(entity.suggested_type)


async def resolve_entities(
    writer: GraphWriter, entities: list[ExtractedEntity]
) -> dict[str, uuid.UUID]:
    """Resolve every extracted entity through the writer, name to resolved content id.

    writer: this chunk's own GraphWriter, already bound to its owner and scope set.
    entities: the extracted, deduplicated-by-name entities to resolve.
    """
    resolved: dict[str, uuid.UUID] = {}
    for entity in entities:
        entity_type = await resolve_entity_type(writer, entity)
        content_id = await writer.resolve(entity.name, entity_type)
        if content_id is not None:
            resolved[entity.name] = content_id
    return resolved


async def write_graph_slice(
    user_id: uuid.UUID,
    chunk: Chunk,
    entities: list[ExtractedEntity],
    dated_facts: list[TimedFact],
) -> set[uuid.UUID]:
    """Resolve and consolidate one chunk's entities and facts, retrying a transient DB conflict.

    A fresh transaction per attempt, so a deadlock or serialization failure (real under this
    graph's own concurrency, see is_transient_db_error) simply reruns the whole idempotent write
    rather than losing the chunk. Every mint GraphWriter performs is ON CONFLICT DO NOTHING or an
    equivalent idempotent upsert, so a retried write is safe.

    user_id: identity that owns the written claims.
    chunk: the chunk being resolved and consolidated.
    entities: this chunk's already-extracted entities, journal and/or LLM sourced.
    dated_facts: this chunk's already-extracted, dated candidate facts.
    """
    resolved: dict[str, uuid.UUID] = {}
    async for attempt in AsyncRetrying(
        retry=retry_if_exception(is_transient_db_error),
        stop=stop_after_attempt(4),
        wait=wait_random_exponential(multiplier=0.05, max=1.0),
        reraise=True,
    ):
        with attempt, span("db_write"):
            async with acting_as(user_id):
                writer = GraphWriter(user_id, tuple(chunk.scopes))
                resolved = await resolve_entities(writer, entities)
                await writer.consolidate_facts(dated_facts, resolved, chunk.id)
                await mark_processed(chunk.id)
    return set(resolved.values())


async def extract_and_consolidate(chunk: Chunk, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Extract, resolve, and consolidate one chunk's graph slice, return the entities it touched.

    The shared per-chunk core both build_graph's concurrent loop and the pgqueuer worker's
    extraction entrypoint call, gated end to end by extraction_semaphore so however many chunks
    are in flight at once, inline or dispatched as concurrent queue jobs, only
    settings.graph_build_concurrency of them ever hit the LLM endpoint at a time.

    journal_extraction runs first and unconditionally. A chunk that clears extract_min_chars, or
    carries no dated line at all, then also runs llm_extraction, its entities and facts folding in
    beside any journal ones rather than replacing them, so a note that mixes prose and a dated log
    gets both. Every path, short-circuited, gated out, successful, fact-free, or an output too
    rich for extract_max_tokens to finish, ends by marking the chunk processed so pending_chunks
    never offers it again. Only a timed-out extraction leaves it pending for a later retry, and an
    unreachable endpoint raises immediately rather than grinding through the rest of the queue.

    chunk: the pending chunk to build a graph slice from.
    user_id: identity that owns the written claims.
    """
    async with extraction_semaphore():
        async with acting_as(user_id):
            document = await session().get(Document, chunk.document_id)
        entities, dated_facts = await journal_extraction(user_id, chunk, document)
        short = len(chunk.text.strip()) < settings.extract_min_chars
        if short and not dated_facts:
            async with acting_as(user_id):
                await mark_processed(chunk.id)
            return set()
        if not short:
            try:
                llm_entities, llm_facts = await llm_extraction(chunk, document)
            except ChunkExtractionTimedOut:
                return set()
            entities = [*entities, *llm_entities]
            dated_facts = [*dated_facts, *llm_facts]
        touched = await write_graph_slice(user_id, chunk, entities, dated_facts)
        logger.info("graph slice from chunk {} done", chunk.id)
        return touched


def raise_unreachable(chunks: list[Chunk], results: list[set[uuid.UUID] | BaseException]) -> None:
    """Re-raise a systemic ExtractionUnreachableError, or log any other chunk failure and move on.

    chunks: the chunks build_graph dispatched, in the same order as results.
    results: each chunk's outcome from asyncio.gather(..., return_exceptions=True).
    """
    for chunk, result in zip(chunks, results, strict=True):
        if isinstance(result, ExtractionUnreachableError):
            raise result
        if isinstance(result, BaseException):
            logger.error("chunk {} failed unexpectedly, skipping: {}", chunk.id, result)


async def build_graph(
    limit: int | None = None,
    user_id: uuid.UUID | None = None,
    source: str | None = None,
) -> tuple[int, int]:
    """Build the graph from chunks the build has never run over and return the counts created.

    Runs extract_and_consolidate over every pending chunk concurrently through asyncio.gather,
    each one individually gated by the shared extraction_semaphore so the LLM endpoint only ever
    sees settings.graph_build_concurrency requests in flight at once regardless of how many chunk
    coroutines are already started and waiting. Each chunk resolves and consolidates on its own
    fresh owner-scoped transaction, so one slow or failed chunk never blocks another's write. An
    unanticipated exception from one chunk is logged and skipped by raise_unreachable rather than
    cancelling every other chunk's own in-flight coroutine, return_exceptions=True's own job.

    limit: maximum number of chunks to process, all of them when null.
    user_id: identity that owns the written claims, the system user when null.
    source: when set, restrict the build to chunks of documents whose title matches this
        substring, so the graph can be grown one source subset at a time.
    """
    user_id = user_id or settings.system_user_id
    # DEFER GraphRAG and LightRAG community detection and summaries, and RAPTOR recursive summary
    # trees, run as a second pass over the graph this builds to serve global queries.
    chunks = await pending_chunks(user_id, limit, source)
    entities_before, facts_before = await graph_counts(user_id)
    results = await asyncio.gather(
        *(extract_and_consolidate(chunk, user_id) for chunk in chunks),
        return_exceptions=True,
    )
    raise_unreachable(chunks, results)
    entities_after, facts_after = await graph_counts(user_id)
    return entities_after - entities_before, facts_after - facts_before


def redirect_entity(
    redirect: dict[uuid.UUID, uuid.UUID | None], entity_id: uuid.UUID | None
) -> tuple[uuid.UUID | None, bool]:
    """Resolve one subject or object id through the duplicate-to-canonical redirect map.

    A null input (a unary fact's absent object) passes through unchanged and never drops. An id
    absent from the map was never a duplicate and also passes through unchanged. An id present but
    mapped to null was a path-like name dropped outright with no canonical replacement, so the fact
    naming it is dangling and the caller drops it rather than repointing to nothing.

    redirect: duplicate content id to its canonical replacement, null for a dropped, unreplaced id.
    entity_id: the fact's own subject_id or object_id to resolve.
    """
    if entity_id is None:
        return None, False
    if entity_id not in redirect:
        return entity_id, False
    replacement = redirect[entity_id]
    return replacement, replacement is None


def claim_row(claim: FactClaim, content_id: uuid.UUID) -> dict:
    """One claim's full column set as a plain dict, content_id re-pointed at the corrected row.

    claim: the claim being migrated onto a corrected fact content row.
    content_id: the corrected content row's own id, the same id the original claim already named.
    """
    return {
        "id": claim.id,
        "content_id": content_id,
        "owner_id": claim.owner_id,
        "scopes": claim.scopes,
        "valid": claim.valid,
        "recorded": claim.recorded,
        "last_accessed": claim.last_accessed,
        "access_count": claim.access_count,
        "attributes": claim.attributes,
        "source_chunk_id": claim.source_chunk_id,
        "promoted_from": claim.promoted_from,
    }


async def snapshot_claims(content_id: uuid.UUID) -> list[dict]:
    """Read and expunge a fact content's whole claim history ahead of its cascading delete.

    Content is immutable under row level security, so a fact naming a duplicate is corrected by
    deleting and re-minting it at the very same content-addressed id rather than an in-place
    UPDATE. Deleting it would otherwise cascade away its claims through their own foreign key, so
    they are snapshotted here first and reinserted verbatim onto the corrected row, their own
    bi-temporal history preserved unchanged. The identity map still tracks a claim as persistent
    once the cascade below removes its physical row, a DB-level FK action the ORM never observes
    on its own, so each is expunged before its replacement reuses the identical claim id.

    content_id: the fact content whose whole claim history, live and superseded, is snapshotted.
    """
    claims = list(
        await session().scalars(
            select(FactClaim)
            .where(FactClaim.content_id == content_id)
            .execution_options(**{settings.skip_live_gate: True})
        )
    )
    saved = [claim_row(claim, content_id) for claim in claims]
    for claim in claims:
        session().expunge(claim)
    return saved


# `repoint_fact_content` below is exercised end to end by
# `test_build.py::test_dedup_merges_a_slug_twin_and_then_converges` and
# `test_dedup_drops_a_dangling_fact_naming_a_dropped_duplicate` (confirmed by direct print-based
# execution tracing and by the tests' own assertions on the corrected row, which only pass because
# this function ran) but a suite-wide coverage run loses line attribution for it specifically, an
# artifact of tracing code that runs on a freshly constructed admin-bypass `AsyncSession` bound to
# its own ad-hoc engine rather than the app's cached one, isolated to this function and not
# reproducible for `graph/reembed.py`'s own ad-hoc-engine session, which the same coverage run
# attributes correctly; running `test_build.py` alone likewise attributes it correctly. Marked
# rather than chased further given the confirmed-correct behavior.
async def repoint_fact_content(  # pragma: no cover
    content_id: uuid.UUID, redirect: dict[uuid.UUID, uuid.UUID | None]
) -> None:
    """Correct one fact content's subject or object off a duplicate, migrating its claims.

    A fact whose subject or object was a dropped, unreplaced duplicate is dangling and removed
    outright instead, its claims cascading away with it since there is nothing left to reinsert
    them onto.

    Runs on the owner-role admin connection `merge_duplicates` opened and bound to context, row
    level security bypassed entirely, since a merge must reach every claim any tenant holds on the
    affected content, never only the pass's own visible slice.

    content_id: the fact content naming at least one duplicate id.
    redirect: duplicate content id to its canonical replacement, null for a dropped id.
    """
    content = await session().get(FactContent, content_id)
    assert content is not None  # read as an affected id in the same admin-bypassed connection
    corrected_subject, subject_dropped = redirect_entity(redirect, content.subject_id)
    corrected_object, object_dropped = redirect_entity(redirect, content.object_id)
    if subject_dropped or object_dropped or corrected_subject is None:
        await session().delete(content)
        return
    saved = await snapshot_claims(content_id)
    await session().delete(content)
    await session().flush()
    session().add(
        FactContent(
            id=content_id,
            subject_id=corrected_subject,
            object_id=corrected_object,
            predicate=content.predicate,
            statement=content.statement,
            embedding=content.embedding,
        )
    )
    await session().flush()
    session().add_all(FactClaim(**row) for row in saved)


async def find_duplicates() -> dict[uuid.UUID, uuid.UUID | None]:
    """Group visible entity content by normalized name and type, return the canonical redirect map.

    The RAPTOR tree's summary nodes are derived and rebuilt wholesale, never knowledge the
    extractor wrote, so they stay out of the dedup that merges and repoints knowledge nodes. The
    earliest id by byte order stays canonical, so a rerun is idempotent and converges. An entity
    whose name normalizes to empty was a path the extractor mistook for a thing, so it and its
    dangling facts are dropped. It names no canonical entry, so every other entity of the same
    empty key redirects to null.
    """
    entities = sorted(
        await session().scalars(
            select(EntityContent).where(EntityContent.type != ontology.RAPTOR_SUMMARY)
        ),
        key=lambda entity: entity.id.bytes,
    )
    canonical: dict[tuple[str, str], uuid.UUID] = {}
    redirect: dict[uuid.UUID, uuid.UUID | None] = {}
    for entity in entities:
        normalized = normalize_name(entity.name)
        keep = canonical.get((entity.type, normalized)) if normalized else None
        if normalized and keep is None:
            canonical[(entity.type, normalized)] = entity.id
            continue
        redirect[entity.id] = keep
    return redirect


async def affected_fact_ids(
    redirect: dict[uuid.UUID, uuid.UUID | None],
) -> list[uuid.UUID]:
    """Fact content naming at least one duplicate the redirect map will correct or drop.

    redirect: duplicate content id to its canonical replacement, from find_duplicates.
    """
    return list(
        await session().scalars(
            select(FactContent.id).where(
                or_(FactContent.subject_id.in_(redirect), FactContent.object_id.in_(redirect))
            )
        )
    )


async def migrate_entity_claims(duplicate_id: uuid.UUID, canonical_id: uuid.UUID) -> None:
    """Repoint a merged-away entity's own claims onto the canonical content before it is deleted.

    `entity_claim.content_id` cascades on delete, so dropping a duplicate content row would
    otherwise take with it the claim of any tenant who staked only that duplicate, leaving them
    seeing neither the duplicate nor the survivor. Each such claim moves to the canonical content
    id instead, so no tenant loses its stake. A claim that would collide with one the same owner
    already holds on the canonical content for the identical scope set is dropped rather than
    duplicated, since that owner already has its canonical claim and the unique key admits one.

    Runs on the owner-role admin connection `merge_duplicates` opened and bound to context, row
    level security bypassed, the cross-tenant reach a structural merge needs to reach every
    holder's claim.

    duplicate_id: the merged-away entity content whose claims are migrated.
    canonical_id: the surviving entity content the claims are repointed onto.
    """
    canonical_claim = aliased(EntityClaim)
    collides_with_canonical = (
        select(canonical_claim.id)
        .where(canonical_claim.content_id == canonical_id)
        .where(canonical_claim.owner_id == EntityClaim.owner_id)
        .where(canonical_claim.scopes == EntityClaim.scopes)
        .exists()
    )
    await session().execute(
        delete(EntityClaim)
        .where(EntityClaim.content_id == duplicate_id, collides_with_canonical)
        .execution_options(synchronize_session=False)
    )
    await session().execute(
        update(EntityClaim)
        .where(EntityClaim.content_id == duplicate_id)
        .values(content_id=canonical_id)
        .execution_options(synchronize_session=False)
    )


async def merge_duplicates(
    affected_ids: list[uuid.UUID], redirect: dict[uuid.UUID, uuid.UUID | None]
) -> int:
    """Repoint every affected fact, migrate each duplicate's claims, and delete the duplicate node.

    Runs entirely on the owner-role admin connection, bypassing row level security. Content's own
    claim-gated SELECT policy would otherwise hide another tenant's private claim on the very
    content this merge must migrate, and content's DELETE policy is admin-gated in the first
    place, so a real structural merge needs the same superuser reach migrations already run with.
    Repointing and deleting run as two separate transactions, since every repoint must land before
    a duplicate's own claims (which repoint_fact_content may have just migrated facts onto) are
    safe to cascade away. In that second transaction each duplicate's own entity claims are moved
    onto its canonical survivor before the duplicate is deleted, so the delete's cascade never
    strips a tenant of the only claim it held.

    affected_ids: fact content naming at least one duplicate, from affected_fact_ids.
    redirect: duplicate content id to its canonical replacement, from find_duplicates.
    """
    merged = 0
    async with bypass_rls() as session:
        async with session.begin():
            for content_id in affected_ids:
                await repoint_fact_content(content_id, redirect)
        async with session.begin():
            for duplicate_id, canonical_id in redirect.items():
                entity = await session.get(EntityContent, duplicate_id)
                if entity is not None:  # pragma: no cover - always true within a single pass
                    if canonical_id is not None:
                        await migrate_entity_claims(duplicate_id, canonical_id)
                    await session.delete(entity)
                    merged += 1
    return merged


async def dedup_entities(user_id: uuid.UUID | None = None) -> int:
    """Merge entity content sharing a normalized name and type, repoint claims, return the count.

    Reads the duplicates under the caller's own row-level-security visibility, then merges them on
    the admin-bypassed connection, the reach a real structural merge needs to migrate every
    tenant's claim, not merely the ones the merging user can itself see.

    user_id: identity whose row level security visibility scopes which content this pass can
        find and merge, the system user when null.
    """
    user_id = user_id or settings.system_user_id
    async with acting_as(user_id):
        redirect = await find_duplicates()
        if not redirect:
            return 0
        affected_ids = await affected_fact_ids(redirect)
    merged = await merge_duplicates(affected_ids, redirect)
    logger.info("deduped {} duplicate entity content rows", merged)
    return merged
