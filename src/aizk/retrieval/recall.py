import time
import uuid
from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from datetime import datetime

from loguru import logger
from mainboard.profiling import span
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import ColumnElement, Result, Row, bindparam, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..graph.algos import ppr_expand
from ..serving import Embedder, Reranker
from ..store import EntityContent, FactClaim, FactContent, Profile, SessionItem, acting_as
from .models import FactHit, Hit, SessionNote


@asynccontextmanager
async def stage(name: str) -> AsyncIterator[None]:
    """Time one recall stage and emit it at DEBUG as `stage=<name> ms=<elapsed>`, spanned too.

    The one instrumentation seam every lane wraps its own work in, so a `settings.log_level=DEBUG`
    run prints the whole per-call latency budget with no separate profiler attached, and, once
    `enable_spans()` is on, the identical window folds into `mainboard.profiling`'s process-wide
    `Collector` under this same name, nested under whatever span is already open on this task's
    stack. A disabled span costs one boolean check, so this stays free by default.

    name: label the stage logs, and spans, under.
    """
    start = time.perf_counter()
    with span(name):
        yield
    logger.debug("stage={} ms={:.1f}", name, (time.perf_counter() - start) * 1000)


def temporal_filter(as_of: datetime | None) -> tuple[list[ColumnElement[bool]], dict[str, bool]]:
    """The extra claim predicates and execution options that gate a fact read at a world-time.

    The live graph relies on the do_orm_execute listener's live gate and adds nothing here; a
    historical replay lists `visible_at(as_of)` itself and opts the listener out, since the as_of
    gate deliberately drops the is_latest filter the live gate keeps.

    as_of: world-time to read the facts at, the live graph when null.
    """
    if as_of is None:
        return [], {}
    return FactClaim.visible_at(as_of), {settings.skip_live_gate: True}


def fact_hits(rows: Result, margin: float | None = None) -> list[FactHit]:
    """Render an ORM facts result over (statement, predicate, valid, distance) as hits.

    Turns cosine distance into a one-minus-distance score and unpacks each valid-time `Range` into
    plain lower and upper bounds. When a margin is given, only facts whose score clears it survive.

    rows: a facts result selecting statement, predicate, valid, and distance.
    margin: minimum score a fact must clear to be kept, no floor when null.
    """
    return [
        FactHit(
            statement=row.statement,
            predicate=row.predicate,
            score=1.0 - row.distance,
            valid_from=row.valid.lower if row.valid else None,
            valid_to=row.valid.upper if row.valid else None,
        )
        for row in rows
        if margin is None or 1.0 - row.distance >= margin
    ]


def sql_fact_hit(row: Row) -> FactHit:
    """Render one hybrid_recall SQL row's fact or neighbor kind as a FactHit.

    The row already names every column the same as FactHit's own fields, so `from_attributes`
    copies it directly with no field-by-field unpacking.

    row: one row from hybrid_recall's fact or neighbor kind.
    """
    return FactHit.model_validate(row, from_attributes=True)


async def hybrid_recall_rows(
    session: AsyncSession, vector: list[float], query: str, k: int
) -> Sequence[Row]:
    """Call the hybrid_recall SQL function, one round trip fusing chunks and live facts.

    Feeds rrf_k and fusion_depth from live settings at call time; the trusted-first promoted bonus
    is baked into the function body instead, since a SQL-language function takes no config of its
    own.

    session: open, principal-scoped session the call runs under, row level security and all.
    vector: dense query embedding.
    query: natural-language search string, the lexical lane's match text.
    k: fused chunk hits and seed facts the function returns per kind.
    """
    async with stage("hybrid_recall_sql"):
        rows = await session.execute(
            text(
                "SELECT * FROM hybrid_recall(:qvec, :qtext, :k, :rrf_k, :fusion_depth)"
            ).bindparams(bindparam("qvec", type_=HALFVEC(len(vector)))),
            {
                "qvec": vector,
                "qtext": query,
                "k": k,
                "rrf_k": settings.rrf_k,
                "fusion_depth": settings.fusion_depth,
            },
        )
    return rows.all()


async def latest_facts(
    session: AsyncSession,
    vector: list[float],
    k: int,
    as_of: datetime | None,
) -> list[FactHit]:
    """Rank the visible latest facts by cosine distance to an already-embedded query vector.

    The as_of-aware ORM path graph_search always reads and a replay round falls back to, since
    hybrid_recall only ever reads the live graph. Joins the claim onto its content since the
    statement, predicate, and embedding live on the deduplicated content while `valid` is the
    claim's own per-container bi-temporal state.

    session: open, principal-scoped session.
    vector: dense query embedding.
    k: number of facts to return.
    as_of: world-time the facts must be valid at, the live graph when null.
    """
    distance = FactContent.embedding.cosine_distance(vector)
    gate, opts = temporal_filter(as_of)
    rows = await session.execute(
        select(
            FactContent.statement,
            FactContent.predicate,
            FactClaim.valid,
            distance.label("distance"),
        )
        .join(FactClaim, FactClaim.content_id == FactContent.id)
        .where(FactContent.embedding.is_not(None), *gate)
        .order_by(distance)
        .limit(k)
        .execution_options(**opts)
    )
    return fact_hits(rows)


async def seed_entities(
    session: AsyncSession, vector: list[float], as_of: datetime | None
) -> tuple[list[uuid.UUID], set[uuid.UUID]]:
    """The closest latest facts' own ids and the entity ids they touch, the multi-hop seed pool.

    Shared by `Recall.neighbor_facts` and `Recall.ppr_facts`, whose own graph_facts_k-wide walk
    both start from this same closest-facts read before diverging into a one-hop join or a
    pagerank expansion.

    session: open, principal-scoped session.
    vector: dense query embedding.
    as_of: world-time the seed facts must be valid at, the live graph when null.
    """
    distance = FactContent.embedding.cosine_distance(vector)
    gate, opts = temporal_filter(as_of)
    rows = await session.execute(
        select(FactContent.id, FactContent.subject_id, FactContent.object_id)
        .join(FactClaim, FactClaim.content_id == FactContent.id)
        .where(FactContent.embedding.is_not(None), *gate)
        .order_by(distance)
        .limit(settings.graph_facts_k)
        .execution_options(**opts)
    )
    seeds = rows.all()
    entities = {row.subject_id for row in seeds} | {
        row.object_id for row in seeds if row.object_id is not None
    }
    return [row.id for row in seeds], entities


async def facts_near(
    session: AsyncSession,
    entity_ids: Collection[uuid.UUID],
    vector: list[float],
    as_of: datetime | None,
    k: int,
    exclude: Collection[uuid.UUID] = (),
    margin: float | None = None,
) -> list[FactHit]:
    """The latest facts touching any of entity_ids, closest to vector, the multi-hop tail query.

    The shared tail `neighbor_facts` and `ppr_facts` both run once their own seed or pagerank-
    expanded entity set is ready, differing only in which entities they pass and whether they
    exclude the seed facts themselves or apply a relevance margin.

    session: open, principal-scoped session.
    entity_ids: entities a fact must touch as subject or object to match.
    vector: dense query embedding.
    as_of: world-time the facts must be valid at, the live graph when null.
    k: number of facts to return.
    exclude: fact ids to drop from the result, the seed facts a neighbor read must not repeat.
    margin: minimum score a fact must clear to be kept, no floor when null.
    """
    distance = FactContent.embedding.cosine_distance(vector)
    gate, opts = temporal_filter(as_of)
    clauses = [
        FactContent.embedding.is_not(None),
        or_(FactContent.subject_id.in_(entity_ids), FactContent.object_id.in_(entity_ids)),
        *gate,
    ]
    if exclude:
        clauses.append(FactContent.id.not_in(exclude))
    rows = await session.execute(
        select(
            FactContent.statement,
            FactContent.predicate,
            FactClaim.valid,
            distance.label("distance"),
        )
        .join(FactClaim, FactClaim.content_id == FactContent.id)
        .where(*clauses)
        .order_by(distance)
        .limit(k)
        .execution_options(**opts)
    )
    return fact_hits(rows, margin=margin)


async def session_hits(
    session: AsyncSession,
    vector: list[float],
    k: int,
) -> list[SessionNote]:
    """Rank the still-working session items by cosine distance to the query, the working lane.

    Reads only the unpromoted items, the ones whose knowledge has not yet reached the graph, so a
    recall never doubly surfaces an item both here and as a promoted fact, and ranks them by the
    same pgvector cosine operator every other lane uses. The session must already be scoped to a
    principal so row level security restricts the rows.

    session: open, principal-scoped session.
    vector: dense query embedding.
    k: number of working items to return.
    """
    distance = SessionItem.embedding.cosine_distance(vector)
    rows = await session.execute(
        select(SessionItem.text, SessionItem.kind, distance.label("distance"))
        .where(SessionItem.embedding.is_not(None), SessionItem.promoted_at.is_(None))
        .order_by(distance)
        .limit(k)
    )
    return [SessionNote(text=row.text, kind=row.kind, score=1.0 - row.distance) for row in rows]


async def top_profile(session: AsyncSession, vector: list[float]) -> str | None:
    """The stored profile of the entity closest to the query, the portrait lane of a recall.

    Ranks the visible profiled entities by embedding distance to the query and returns the best
    match's rolled-up summary, so a recall about a known subject opens with its portrait rather
    than reassembling identity from individual facts. Null when nothing visible is profiled.

    session: open, principal-scoped session.
    vector: dense query embedding.
    """
    return await session.scalar(
        select(Profile.summary)
        .join(EntityContent, EntityContent.id == Profile.subject_id)
        .where(EntityContent.embedding.is_not(None))
        .order_by(EntityContent.embedding.cosine_distance(vector))
        .limit(1)
    )


async def graph_search(
    query: str,
    k: int = 20,
    principal_id: uuid.UUID | None = None,
    as_of: datetime | None = None,
) -> list[FactHit]:
    """Search the knowledge graph and return the top latest facts by embedding.

    Embeds the query and ranks the row-level-security-visible facts where is_latest by cosine
    distance, keeping only those valid at as_of when one is given so the answer reflects the
    graph's state at that world-time.

    query: natural-language search string.
    k: number of facts to return.
    principal_id: identity whose row level security visibility scopes the results, the system
        principal when null.
    as_of: world-time the facts must be valid at, the live graph when null.
    """
    principal_id = principal_id or settings.system_principal_id
    [vector] = await Embedder().embed([query], mode="query")
    async with acting_as(principal_id) as session:
        hits = await latest_facts(session, vector, k, as_of)
    logger.info("graph search for {query!r} returned {count} facts", query=query, count=len(hits))
    return hits


async def rerank_hits(query: str, pool: list[Hit], k: int) -> list[Hit]:
    """Reorder the fused pool with a cross-encoder and keep the top k.

    Scores each candidate text directly against the query so the final ranking reflects
    query-document interaction rather than the rank-only fusion, then truncates to k. Each
    candidate is truncated to rerank_snippet_chars before scoring, the returned Hit's own text
    unaffected, since the endpoint's own latency scales with candidate length and a cross-encoder's
    verdict is dominated by a passage's early tokens anyway.

    query: natural-language search string.
    pool: fused candidates to rescore, ordered best first.
    k: number of results to return.
    """
    snippets = [hit.text[: settings.rerank_snippet_chars] for hit in pool]
    async with stage("rerank_http"):
        scores = await Reranker().rerank(query, snippets)
    rescored = [
        hit.model_copy(update={"score": score}) for hit, score in zip(pool, scores, strict=True)
    ]
    rescored.sort(key=lambda hit: hit.score, reverse=True)
    return rescored[:k]


async def search(
    query: str,
    k: int = 8,
    principal_id: uuid.UUID | None = None,
) -> list[Hit]:
    """Run hybrid dense and lexical search over the live graph and return the top k hits.

    Embeds the query once and reads the fused, promoted-bonused chunk hits through the
    hybrid_recall SQL function on a principal-scoped session, reranking with the cross-encoder
    before truncating when enabled.

    query: natural-language search string.
    k: number of results to return.
    principal_id: identity whose row level security visibility scopes the results, the system
        principal when null.
    """
    principal_id = principal_id or settings.system_principal_id
    embedder = Embedder()
    [vector] = await embedder.embed([query], mode="query")
    async with acting_as(principal_id) as session:
        round_ = Recall(session, embedder, query, vector, k, None, ppr=False)
        hits, _, _ = await round_.hybrid_recall()
    return await rerank_hits(query, hits, k) if settings.rerank else hits


def merge_facts(base: list[FactHit], extra: list[FactHit]) -> list[FactHit]:
    """Append the extra facts not already present by statement, keeping base order ahead of extra.

    base: the facts already assembled, kept first.
    extra: candidate facts, each added only when its statement is new.
    """
    seen = {fact.statement for fact in base}
    merged = list(base)
    for fact in extra:
        if fact.statement not in seen:
            seen.add(fact.statement)
            merged.append(fact)
    return merged


def merge_hits(base: list[Hit], extra: list[Hit], limit: int) -> list[Hit]:
    """Append the extra hits not already present by text, capped at limit, base order kept first.

    base: the hits already assembled, kept first.
    extra: candidate hits, each added only when its text is new.
    limit: ceiling on the merged list so the extra round cannot grow the context without bound.
    """
    seen = {hit.text for hit in base}
    merged = list(base)
    for hit in extra:
        if hit.text not in seen:
            seen.add(hit.text)
            merged.append(hit)
    return merged[:limit]


def expand_query(query: str, hits: list[Hit], facts: list[FactHit]) -> str:
    """Widen a thin query with the best evidence already recalled, the second-round seed.

    Appends the top fact statements and hit snippets to the original query so the extra round
    reaches the neighborhood the first matches point at, the IRIS evidence-gap re-retrieval seed.
    Returns the query unchanged when nothing was recalled to seed the widening from.

    query: the original natural-language query.
    hits: the hits the first round recalled, best first.
    facts: the facts the first round recalled, best first.
    """
    seeds = [fact.statement for fact in facts[: settings.gap_seed_terms]]
    seeds += [hit.text for hit in hits[: settings.gap_seed_terms]]
    return " ".join([query, *seeds]) if seeds else query


async def has_evidence_gap(query: str, hits: list[Hit], facts: list[FactHit]) -> bool:
    """Whether the recalled context is thin enough to warrant one targeted extra round.

    Cheap by default, reading the hit count and, when recall_gap_min_score is set, the best hit
    score, so the common path pays no model call. When recall_gap_judge is on it additionally asks
    the LLM judge whether the rendered context answers the query, the costlier IRIS-style signal.

    query: the natural-language query the context must answer.
    hits: the hits the first round recalled.
    facts: the facts the first round recalled.
    """
    if len(hits) < settings.recall_gap_min_hits:
        return True
    if settings.recall_gap_min_score > 0.0:
        best = max((hit.score for hit in hits), default=0.0)
        if best < settings.recall_gap_min_score:
            return True
    if settings.recall_gap_judge:
        from ..eval import judge_answerable

        context = "\n".join(
            [f"({fact.predicate}) {fact.statement}" for fact in facts] + [hit.text for hit in hits]
        )
        return not await judge_answerable(query, context)
    return False


def log_gap_fill(query: str, added_hits: int, added_facts: int) -> None:
    """Log how many new hits and facts one gap-fill round added, the diagnostic fill_gap emits.

    query: the original query the gap-fill round widened.
    added_hits: hits the merge added beyond the first round's own.
    added_facts: facts the merge added beyond the first round's own.
    """
    logger.info(
        "gap fill for {query!r} added {h} hits and {f} facts",
        query=query,
        h=added_hits,
        f=added_facts,
    )


class Recall:
    """One recall round bound to the session, query, and lane toggles its helpers otherwise repeat.

    Binds the shared (session, vector, k, as_of) tuple once so each method reads it off `self`
    instead of re-threading it through every call.

    session: open, principal-scoped session.
    embedder: the embedder an evidence-gap round re-embeds its expanded query with.
    query: natural-language search string, also the lexical and rerank text.
    vector: dense query embedding.
    k: number of fused hits and of seed facts to surface.
    as_of: world-time the facts must be valid at, the live graph when null.
    ppr: whether the multi-hop personalized-pagerank lane widens the seed and neighbor facts.
    """

    def __init__(
        self,
        session: AsyncSession,
        embedder: Embedder,
        query: str,
        vector: list[float],
        k: int,
        as_of: datetime | None,
        ppr: bool,
    ) -> None:
        self.session, self.embedder, self.query = session, embedder, query
        self.vector, self.k, self.as_of, self.ppr = vector, k, as_of, ppr

    async def top_profile(self) -> str | None:
        """The stored profile of the entity closest to the query, the portrait lane of a recall."""
        return await top_profile(self.session, self.vector)

    async def hybrid_recall(self) -> tuple[list[Hit], list[FactHit], list[FactHit]]:
        """Run the hybrid_recall SQL function once and split its rows into hits, seeds, neighbors.

        Widens the chunk pool to rerank_candidates when rerank is on so the cross-encoder has real
        candidates to choose from; the fact and neighbor lanes stay capped at this round's k either
        way, since only the chunk lane is ever reranked.
        """
        depth = settings.rerank_candidates if settings.rerank else self.k
        rows = await hybrid_recall_rows(self.session, self.vector, self.query, depth)
        hits = [
            Hit.model_validate(row, from_attributes=True) for row in rows if row.kind == "chunk"
        ]
        seeds = [sql_fact_hit(row) for row in rows if row.kind == "fact"][: self.k]
        neighbors = [sql_fact_hit(row) for row in rows if row.kind == "neighbor"][: self.k]
        return hits, seeds, neighbors

    async def latest_facts(self) -> list[FactHit]:
        """The closest latest facts visible at this round's as_of, the as_of replay's seed lane."""
        return await latest_facts(self.session, self.vector, self.k, self.as_of)

    async def session_hits(self, k: int) -> list[SessionNote]:
        """The still-working session items ranked against this round's vector, the working lane.

        k: number of working items to return, independent of this round's fused-hit k.
        """
        return await session_hits(self.session, self.vector, k)

    async def neighbor_facts(self) -> list[FactHit]:
        """The one-hop neighbor facts of this round's closest latest facts, excluding the seeds."""
        seed_ids, entity_ids = await seed_entities(self.session, self.vector, self.as_of)
        if not entity_ids:
            return []
        return await facts_near(
            self.session, entity_ids, self.vector, self.as_of, self.k, exclude=seed_ids
        )

    async def ppr_facts(self) -> list[FactHit]:
        """The facts personalized pagerank reaches from this round's seed entities.

        Widens the seed entities to the associatively related ones a multi-hop walk keeps
        returning to, then keeps only the reached facts whose own score clears ppr_margin, the
        HippoRAG lane past the one-hop neighborhood `neighbor_facts` covers.
        """
        _, entity_ids = await seed_entities(self.session, self.vector, self.as_of)
        expanded = (
            await ppr_expand(self.session, list(entity_ids), top_n=settings.graph_facts_k)
            if entity_ids
            else []
        )
        if not expanded:
            return []
        return await facts_near(
            self.session, expanded, self.vector, self.as_of, self.k, margin=settings.ppr_margin
        )

    async def replay_seeds(
        self, seeds: list[FactHit], neighbors: list[FactHit]
    ) -> tuple[list[FactHit], list[FactHit]]:
        """Swap in the as_of-aware ORM seed and neighbor lanes for a historical replay round.

        seeds: the live hybrid_recall seed facts, kept as-is for a live (non-replay) round.
        neighbors: the live hybrid_recall neighbor facts, kept as-is for a live round.
        """
        if self.as_of is None:
            return seeds, neighbors
        async with stage("as_of_seed_lanes"):
            return await self.latest_facts(), await self.neighbor_facts()

    async def multihop_facts(self) -> list[FactHit]:
        """The pagerank-reached facts when this round's ppr lane is on, empty otherwise."""
        if not self.ppr:
            return []
        async with stage("ppr_facts"):
            return await self.ppr_facts()

    async def reranked_hits(self, hits: list[Hit]) -> list[Hit]:
        """This round's chunk hits, reranked once the pool clears rerank_min_pool, else truncated.

        hits: the hybrid_recall chunk pool, already widened to rerank_candidates when rerank is on.
        """
        if settings.rerank and len(hits) > settings.rerank_min_pool:
            return await rerank_hits(self.query, hits, self.k)
        return hits[: self.k]

    async def assemble_context(self) -> tuple[list[Hit], list[FactHit]]:
        """Build one recall round: hybrid hits and facts, the as_of replay, ppr, then rerank.

        The live default round reads hits, seeds, and neighbors off one hybrid_recall call, and
        `replay_seeds` swaps in the as_of-aware ORM path only for a historical round, since
        hybrid_recall only ever widens the live graph.
        """
        hits, seeds, neighbors = await self.hybrid_recall()
        seeds, neighbors = await self.replay_seeds(seeds, neighbors)
        multihop = await self.multihop_facts()
        hits = await self.reranked_hits(hits)
        return hits, merge_facts(seeds, neighbors + multihop)

    async def expanded_vector(self, expanded_query: str) -> list[float]:
        """This round's own vector when the gap-fill query is unchanged, else a fresh embed of it.

        expanded_query: the query `expand_query` widened, possibly identical to this round's own.
        """
        if expanded_query == self.query:
            return self.vector
        async with stage("gap_fill_embed"):
            [vector] = await self.embedder.embed([expanded_query], mode="query")
        return vector

    async def fill_gap(
        self, hits: list[Hit], facts: list[FactHit]
    ) -> tuple[list[Hit], list[FactHit]]:
        """Run one extra targeted round when the first recall left a gap, then merge it in.

        Expands the query with the best evidence already found, assembles the hit and fact lanes
        through a second Recall bound to the expanded query on the same shared session, and merges
        only the new items so a thin first recall widens once. Bounded to this single round so
        latency stays sane, the EviMem-style re-retrieval.

        hits: the hits the first round recalled.
        facts: the facts the first round recalled.
        """
        expanded_query = expand_query(self.query, hits, facts)
        vector = await self.expanded_vector(expanded_query)
        more_hits, more_facts = await Recall(
            self.session, self.embedder, expanded_query, vector, self.k, self.as_of, self.ppr
        ).assemble_context()
        merged_hits, merged_facts = (
            merge_hits(hits, more_hits, settings.rerank_candidates),
            merge_facts(facts, more_facts),
        )
        log_gap_fill(self.query, len(merged_hits) - len(hits), len(merged_facts) - len(facts))
        return merged_hits, merged_facts
