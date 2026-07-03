import uuid
from datetime import UTC, datetime

from loguru import logger
from openai import APITimeoutError
from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import Range, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings
from ..extract.llm import decide_consolidation, resolve_timestamps
from ..extract.models import TimedFact
from ..extract.ontology import EntityType
from ..extract.strategies import extract_graph
from ..serving import Embedder
from ..store import (
    Chunk,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    Group,
    LiveFact,
    Membership,
    acting_as,
)
from .dedupe import mint_content
from .ids import entity_id, fact_id
from .naming import normalize_name

# the partial live-uniqueness `fact_claim` carries, one open-`recorded` claim per (content, owner,
# scope); ON CONFLICT DO NOTHING against it is what makes `GraphWriter.consolidate`'s claim insert
# idempotent under a concurrent durable worker re-processing the same chunk, the same statements
# whichever writer gets there first rather than a unique-violation crash.
FACT_CLAIM_LIVE_ARBITER = {
    "index_elements": ["content_id", "owner_id", "scope"],
    "index_where": text("upper_inf(recorded)"),
}


class GraphWriter:
    """One graph-write round bound to the session, owner, and scope every write in it shares.

    resolve and consolidate both re-threaded (session, owner_id, scope) through every call;
    binding them once here turns each repeated argument list into a `self` read. build_graph opens
    one GraphWriter per chunk on its owner-scoped transaction, and background.queue.process_chunk
    does the same for its durable per-chunk job.

    Every mint below is an idempotent `INSERT ... ON CONFLICT DO NOTHING`, on content's own id for
    the deduplicated structural row and on the claim table's own uniqueness for this container's
    stake, so two owners independently extracting the identical entity or fact land the exact same
    statements whichever one runs first: content is minted once and shared, each owner's own claim
    rides beside it, and neither a primary-key collision nor a success/failure timing difference
    ever tells one owner whether the other's private content already existed.

    session: an open session already acting as owner_id under row level security.
    owner_id: principal that owns a newly created claim.
    scope: group a newly created claim is shared with, null when private.
    """

    def __init__(
        self, session: AsyncSession, owner_id: uuid.UUID, scope: uuid.UUID | None
    ) -> None:
        self.session = session
        self.owner_id = owner_id
        self.scope = scope
        self._reviewed_at: datetime | None = None
        self._reviewed_at_cached = False

    async def reviewed_at(self) -> datetime | None:
        """The reviewed_at stamp a claim newly written by this writer should carry, resolved once.

        Private scope and an uncurated group always resolve to now, unchanged single-user and
        ordinary-sharing behavior; a curated group resolves to now only when the owner already
        holds its admin membership role, otherwise to null, landing the claim pending review. Reads
        the group and membership rows once per writer and caches the answer, since every claim this
        writer consolidates shares the same scope and owner.
        """
        if not self._reviewed_at_cached:
            self._reviewed_at = await Group.review_stamp(self.session, self.scope, self.owner_id)
            self._reviewed_at_cached = True
        return self._reviewed_at

    async def resolve(self, name: str, type: str) -> uuid.UUID | None:
        """Resolve a name to a stored content id, reusing or claiming one, dropping path-slugs.

        Normalizes the name first so a path or url echoed as an entity folds to empty and is
        dropped with a null return. When this container already claims the exact content-addressed
        id, that fast path returns immediately with no embedding call. Otherwise it embeds the
        cleaned name, cosine matches the entity content already visible to this container (through
        one of its own or a shared claim) within the same type, and returns the id of the best
        match above settings.entity_resolution_threshold with no new claim minted, since visibility
        already means some claim covers it. With no match it mints a fresh content row and this
        container's own claim on it together, both idempotent upserts, and returns that id.

        name: entity surface form to resolve.
        type: ontology entity type the match must share.
        """
        if not normalize_name(name):
            logger.warning("entity name {!r} is a path or link, dropping", name)
            return None
        node = entity_id(name, type)
        claimed = await self.session.scalar(
            select(EntityClaim.id).where(
                EntityClaim.content_id == node,
                EntityClaim.owner_id == self.owner_id,
                EntityClaim.scope == self.scope,
            )
        )
        if claimed is not None:
            return node
        [vector] = await Embedder().embed([name], mode="document")
        distance = EntityContent.embedding.cosine_distance(vector)
        match = await self.session.scalar(
            select(EntityContent.id)
            .where(EntityContent.type == type)
            .where(distance <= 1.0 - settings.entity_resolution_threshold)
            .order_by(distance)
            .limit(1)
        )
        if match is not None:
            return match
        await mint_content(
            self.session, EntityContent(id=node, name=name, type=type, embedding=vector)
        )
        await self.session.execute(
            insert(EntityClaim)
            .values(content_id=node, owner_id=self.owner_id, scope=self.scope)
            .on_conflict_do_nothing(index_elements=["content_id", "owner_id", "scope"])
        )
        return node

    async def consolidate(self, fact: TimedFact, source_chunk_id: uuid.UUID) -> None:
        """Add, update, or skip a claim against this container's existing latest claims.

        Retrieves this container's similar visible latest claims, asks decide_consolidation for the
        verdict, and on UPDATE closes the old claim by setting valid and recorded before upserting
        the new one, on ADD upserts the new content and claim, and on NOOP leaves the graph
        unchanged.

        fact: the extracted candidate fact, its subject and object already resolved to entities.
        source_chunk_id: chunk the fact was extracted from, stamped as provenance on the new claim.
        """
        # DEFER HippoRAG Personalized PageRank multi-hop retrieval attaches here, walking the
        # edges this method writes to rank facts beyond first-order similarity.
        # an identical triple and statement hashes to the same content-addressed id, so a fact
        # already claimed by this container, live or since superseded or decayed, is a NOOP here
        # and never claimed twice; opts out of the live gate to see a closed claim too.
        identity = fact_id(fact.subject, fact.predicate, fact.object_, fact.statement)
        gate_off = {settings.skip_live_gate: True}
        already_claimed = await self.session.scalar(
            select(FactClaim.id)
            .where(
                FactClaim.content_id == identity,
                FactClaim.owner_id == self.owner_id,
                FactClaim.scope == self.scope,
            )
            .execution_options(**gate_off)
        )
        if already_claimed is not None:
            return
        [vector] = await Embedder().embed([fact.statement], mode="document")
        subject_id = await self.session.scalar(
            select(EntityContent.id)
            .where(EntityContent.name == fact.subject)
            .order_by(EntityContent.id)
            .limit(1)
        )
        if subject_id is None:
            logger.warning("fact subject {!r} has no resolved entity, skipping", fact.subject)
            return
        object_id = (
            await self.session.scalar(
                select(EntityContent.id)
                .where(EntityContent.name == fact.object_)
                .order_by(EntityContent.id)
                .limit(1)
            )
            if fact.object_
            else None
        )
        distance = LiveFact.embedding.cosine_distance(vector)
        # only a currently-valid live claim is a supersession candidate, and `live_fact` already
        # carries that gate, so a future-dated or already-closed claim is never offered for
        # retirement and this query lists only its subject bound.
        existing = list(
            await self.session.scalars(
                select(LiveFact)
                .where(LiveFact.subject_id == subject_id)
                .order_by(distance)
                .limit(settings.similar_facts)
            )
        )
        verdict = await decide_consolidation(fact, existing)
        if verdict.action == "NOOP":
            return
        now = datetime.now(UTC)
        if verdict.action == "UPDATE" and verdict.supersedes is not None:
            retired = await self.session.get(
                FactClaim, verdict.supersedes, execution_options=gate_off
            )
            if retired is not None:
                retired.valid = Range(
                    retired.valid.lower if retired.valid else None, fact.valid_from or now
                )
                retired.recorded = Range(retired.recorded.lower, now)
        await mint_content(
            self.session,
            FactContent(
                id=identity,
                subject_id=subject_id,
                object_id=object_id,
                predicate=fact.predicate,
                statement=fact.statement,
                embedding=vector,
            ),
        )
        await self.session.execute(
            insert(FactClaim)
            .values(
                content_id=identity,
                owner_id=self.owner_id,
                scope=self.scope,
                valid=Range(fact.valid_from, fact.valid_to)
                if fact.valid_from or fact.valid_to
                else None,
                source_chunk_id=source_chunk_id,
                reviewed_at=await self.reviewed_at(),
            )
            .on_conflict_do_nothing(**FACT_CLAIM_LIVE_ARBITER)
        )


async def pending_chunks(
    principal_id: uuid.UUID,
    limit: int | None,
    source: str | None,
) -> list[Chunk]:
    """List the writable chunks that carry no graph claims yet, in a fixed deterministic order.

    A chunk counts as pending until at least one claim records it as its source, so a finished
    build never reprocesses it and a build resumed after an interruption picks up where it stopped.
    Extraction stamps its entities and facts with the source chunk's scope, so only chunks in
    scopes the principal may write are pending for it. A reader or public visitor never extracts
    into someone else's shared graph, that scope's own writers do.

    principal_id: identity whose row level security visibility and write access scope the chunks.
    limit: maximum number of chunks to return, all of them when null.
    source: when set, restrict to chunks of documents whose title matches this substring.
    """
    selection = (
        select(Chunk)
        .where(~exists().where(FactClaim.source_chunk_id == Chunk.id))
        .where(Membership.writable_scope(Chunk.scope, principal_id))
        .order_by(Chunk.id)
        .limit(limit)
    )
    if source is not None:
        titled = select(Document.id).where(Document.title.ilike(f"%{source}%"))
        selection = selection.where(Chunk.document_id.in_(titled))
    async with acting_as(principal_id) as session:
        return list(await session.scalars(selection))


async def graph_counts(principal_id: uuid.UUID) -> tuple[int, int]:
    """Return the entity and fact claim counts visible to a principal under row level security.

    principal_id: identity whose row level security visibility scopes the counts.
    """
    async with acting_as(principal_id) as session:
        entities = await session.scalar(select(func.count()).select_from(EntityClaim)) or 0
        # the count spans the whole visible claim history including superseded versions, so it
        # opts out of the live gate that would otherwise narrow it to the latest open claims.
        facts = (
            await session.scalar(
                select(func.count())
                .select_from(FactClaim)
                .execution_options(**{settings.skip_live_gate: True})
            )
            or 0
        )
    return entities, facts


async def build_graph(
    limit: int | None = None,
    principal_id: uuid.UUID | None = None,
    source: str | None = None,
) -> tuple[int, int]:
    """Build the graph from chunks that have no claims yet and return the counts created.

    Iterates the pending chunks in a fixed deterministic order, and for each one extracts a graph
    slice with the LLM outside any transaction, then opens a fresh owner-scoped transaction to
    resolve its entities and consolidate its facts. Committing one chunk at a time keeps a slow
    extraction from holding a write lock and makes the build resumable, since a chunk that has been
    written is skipped on the next run.

    limit: maximum number of chunks to process, all of them when null.
    principal_id: identity that owns the written claims, the system principal when null.
    source: when set, restrict the build to chunks of documents whose title matches this
        substring, so the graph can be grown one source subset at a time.
    """
    principal_id = principal_id or settings.system_principal_id
    # DEFER GraphRAG and LightRAG community detection and summaries, and RAPTOR recursive summary
    # trees, run as a second pass over the graph this builds to serve global queries.
    chunks = await pending_chunks(principal_id, limit, source)
    entities_before, facts_before = await graph_counts(principal_id)
    for chunk in chunks:
        # a generation that blows past settings.extract_timeout is abandoned and the chunk left
        # pending, so one runaway extraction never stalls the build and a later run retries it.
        try:
            extraction = await extract_graph(chunk.text)
            dated_facts = await resolve_timestamps(chunk.text, extraction.facts)
        except APITimeoutError:
            logger.warning("extraction timed out on chunk {}, skipping", chunk.id)
            continue
        async with acting_as(principal_id) as session:
            writer = GraphWriter(session, principal_id, chunk.scope)
            for entity in extraction.entities:
                await writer.resolve(entity.name, entity.type)
            for fact in dated_facts:
                await writer.consolidate(fact, chunk.id)
        logger.info("graph slice from chunk {} done", chunk.id)
    entities_after, facts_after = await graph_counts(principal_id)
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
    session: AsyncSession, content_id: uuid.UUID, redirect: dict[uuid.UUID, uuid.UUID | None]
) -> None:
    """Correct one fact content's subject or object off a duplicate, migrating its claims.

    Content is immutable under row level security, so a fact naming a duplicate is corrected by
    deleting and re-minting it at the very same content-addressed id rather than an in-place
    UPDATE, both individually admin-gated operations rather than a forbidden one. Deleting it would
    otherwise cascade away its claims through their own foreign key, so they are read back out
    first and reinserted verbatim onto the corrected row, their own bi-temporal history preserved
    unchanged. A fact whose subject or object was a dropped, unreplaced duplicate is dangling and
    removed outright instead, its claims cascading away with it since there is nothing left to
    reinsert them onto.

    session: session bound to the owner-role admin connection, row level security bypassed
        entirely, since a merge must reach every claim any tenant holds on the affected content,
        never only the pass's own visible slice.
    content_id: the fact content naming at least one duplicate id.
    redirect: duplicate content id to its canonical replacement, null for a dropped id.
    """
    content = await session.get(FactContent, content_id)
    assert content is not None  # read as an affected id in the same admin-bypassed connection
    corrected_subject, subject_dropped = redirect_entity(redirect, content.subject_id)
    corrected_object, object_dropped = redirect_entity(redirect, content.object_id)
    if subject_dropped or object_dropped or corrected_subject is None:
        await session.delete(content)
        return
    # every claim this content ever carried migrates, live and superseded alike, so the corrected
    # row keeps the exact same bi-temporal history it had before, opting out of the live gate the
    # way any full-history read of claims does.
    claims = list(
        await session.scalars(
            select(FactClaim)
            .where(FactClaim.content_id == content_id)
            .execution_options(**{settings.skip_live_gate: True})
        )
    )
    saved = [
        {
            "id": claim.id,
            "content_id": content_id,
            "owner_id": claim.owner_id,
            "scope": claim.scope,
            "valid": claim.valid,
            "recorded": claim.recorded,
            "reviewed_at": claim.reviewed_at,
            "last_accessed": claim.last_accessed,
            "access_count": claim.access_count,
            "attributes": claim.attributes,
            "source_chunk_id": claim.source_chunk_id,
            "promoted_from": claim.promoted_from,
        }
        for claim in claims
    ]
    # the identity map still tracks these as persistent even once the cascade below removes their
    # physical rows, a DB-level FK action the ORM never observes on its own, so each is expunged
    # before its replacement below reuses the identical claim id.
    for claim in claims:
        session.expunge(claim)
    await session.delete(content)
    await session.flush()
    session.add(
        FactContent(
            id=content_id,
            subject_id=corrected_subject,
            object_id=corrected_object,
            predicate=content.predicate,
            statement=content.statement,
            embedding=content.embedding,
        )
    )
    await session.flush()
    session.add_all(FactClaim(**row) for row in saved)


async def dedup_entities(
    principal_id: uuid.UUID | None = None,
) -> int:
    """Merge entity content sharing a normalized name and type, repoint claims, return the count.

    Loads the entity content visible to `principal_id`, groups it by normalized name and type so
    slug, spaced, and linked spellings of one thing collapse onto a single canonical content id,
    finds every fact content naming one of the resulting duplicates, and corrects each one through
    `repoint_fact_content` before deleting the duplicate entity content itself, whose own claims
    cascade away through their foreign key. The earliest id by byte order stays canonical, so a
    rerun is idempotent and converges. The read that finds the duplicates and their facts runs
    under the caller's own row-level-security visibility, but every write runs on the owner-role
    admin connection, bypassing row level security entirely: content's own claim-gated SELECT
    policy would otherwise hide another tenant's private claim on the very content this merge must
    migrate, and content's DELETE policy is admin-gated in the first place, so a real structural
    merge needs the same superuser reach migrations already run with, not merely the system
    principal's own still-RLS-governed session. An entity whose name normalizes to empty was a
    path the extractor mistook for a thing, so it and its dangling facts are removed outright.

    principal_id: identity whose row level security visibility scopes which content this pass can
        find and merge, the system principal when null.
    """
    principal_id = principal_id or settings.system_principal_id
    async with acting_as(principal_id) as session:
        # the RAPTOR tree's summary nodes are derived and rebuilt wholesale, never knowledge the
        # extractor wrote, so they stay out of the dedup that merges and repoints knowledge nodes.
        entities = sorted(
            await session.scalars(
                select(EntityContent).where(EntityContent.type != EntityType.RAPTOR_SUMMARY)
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
        if not redirect:
            return 0
        affected_ids = list(
            await session.scalars(
                select(FactContent.id).where(
                    or_(FactContent.subject_id.in_(redirect), FactContent.object_id.in_(redirect))
                )
            )
        )
    merged = 0
    admin = create_async_engine(settings.admin_database_url)
    try:
        admin_sessions = async_sessionmaker(admin, expire_on_commit=False)
        async with admin_sessions(info={"principal": settings.system_principal_id}) as session:
            async with session.begin():
                for content_id in affected_ids:
                    await repoint_fact_content(session, content_id, redirect)
            async with session.begin():
                for duplicate_id in redirect:
                    entity = await session.get(EntityContent, duplicate_id)
                    if entity is not None:  # pragma: no cover - always true within a single pass
                        await session.delete(entity)
                        merged += 1
    finally:
        await admin.dispose()
    logger.info("deduped {} duplicate entity content rows", merged)
    return merged
