import uuid
from collections.abc import Sequence
from datetime import datetime

from loguru import logger
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import ColumnElement, Result, Row, bindparam, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..graph.algos import ppr_expand
from ..graph.communities import community_search
from ..graph.raptor import raptor_search
from ..serving import Embedder, Reranker
from ..store import EntityContent, FactClaim, FactContent, Profile, SessionItem, acting_as
from .models import CommunityNote, FactHit, Hit, RaptorNote, RecallResult, SessionNote
from .query_route import QueryRoute


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
    rows = await session.execute(
        text("SELECT * FROM hybrid_recall(:qvec, :qtext, :k, :rrf_k, :fusion_depth)").bindparams(
            bindparam("qvec", type_=HALFVEC(len(vector)))
        ),
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
    embedder = Embedder()
    [vector] = await embedder.embed([query], mode="query")
    async with acting_as(principal_id) as session:
        hits = await latest_facts(session, vector, k, as_of)
    logger.info("graph search for {query!r} returned {count} facts", query=query, count=len(hits))
    return hits


async def rerank_hits(query: str, pool: list[Hit], k: int) -> list[Hit]:
    """Reorder the fused pool with a cross-encoder and keep the top k.

    Scores each candidate text directly against the query so the final ranking reflects
    query-document interaction rather than the rank-only fusion, then truncates to k.

    query: natural-language search string.
    pool: fused candidates to rescore, ordered best first.
    k: number of results to return.
    """
    reranker = Reranker()
    scores = await reranker.rerank(query, [hit.text for hit in pool])
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
        self.session = session
        self.embedder = embedder
        self.query = query
        self.vector = vector
        self.k = k
        self.as_of = as_of
        self.ppr = ppr

    async def top_profile(self) -> str | None:
        """The stored profile of the entity closest to the query, the portrait lane of a recall.

        Ranks the visible profiled entities by embedding distance to the query and returns the best
        match's rolled-up summary, so a recall about a known subject opens with its portrait rather
        than reassembling identity from individual facts. Null when nothing visible is profiled.
        """
        return await self.session.scalar(
            select(Profile.summary)
            .join(EntityContent, EntityContent.id == Profile.subject_id)
            .where(EntityContent.embedding.is_not(None))
            .order_by(EntityContent.embedding.cosine_distance(self.vector))
            .limit(1)
        )

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
        """Return the one-hop neighbor facts of the closest latest facts within this round.

        Ranks the visible latest facts by distance to the embedded query, treats the closest
        settings.graph_facts_k as seeds, collects the entities those seeds touch, then returns the
        other latest facts adjacent to any of them. The as_of-aware ORM path assemble_context falls
        back to for a replay round, since hybrid_recall only ever widens the live graph.
        """
        distance = FactContent.embedding.cosine_distance(self.vector)
        gate, opts = temporal_filter(self.as_of)
        seeds = (
            await self.session.execute(
                select(FactContent.id, FactContent.subject_id, FactContent.object_id)
                .join(FactClaim, FactClaim.content_id == FactContent.id)
                .where(FactContent.embedding.is_not(None), *gate)
                .order_by(distance)
                .limit(settings.graph_facts_k)
                .execution_options(**opts)
            )
        ).all()
        if not seeds:
            return []
        seed_ids = [row.id for row in seeds]
        entity_ids = {row.subject_id for row in seeds} | {
            row.object_id for row in seeds if row.object_id is not None
        }
        rows = await self.session.execute(
            select(
                FactContent.statement,
                FactContent.predicate,
                FactClaim.valid,
                distance.label("distance"),
            )
            .join(FactClaim, FactClaim.content_id == FactContent.id)
            .where(
                FactContent.embedding.is_not(None),
                FactContent.id.not_in(seed_ids),
                or_(FactContent.subject_id.in_(entity_ids), FactContent.object_id.in_(entity_ids)),
                *gate,
            )
            .order_by(distance)
            .limit(self.k)
            .execution_options(**opts)
        )
        return fact_hits(rows)

    async def ppr_facts(self) -> list[FactHit]:
        """Return the facts personalized pagerank reaches from this round's seed entities.

        Collects the entity ids the closest valid facts touch, runs ppr_expand to widen them to the
        associatively related entities a multi-hop walk keeps returning to, then ranks the facts
        visible at as_of adjacent to those entities by distance to the query, keeping only those
        whose own statement clears ppr_margin so the lane widens reach without surfacing
        off-topic hub facts. This is the HippoRAG lane, the facts past the one-hop neighborhood
        neighbor_facts covers.
        """
        distance = FactContent.embedding.cosine_distance(self.vector)
        gate, opts = temporal_filter(self.as_of)
        seeds = await self.session.execute(
            select(FactContent.subject_id, FactContent.object_id)
            .join(FactClaim, FactClaim.content_id == FactContent.id)
            .where(FactContent.embedding.is_not(None), *gate)
            .order_by(distance)
            .limit(settings.graph_facts_k)
            .execution_options(**opts)
        )
        seed_ids: set[uuid.UUID] = set()
        for row in seeds:
            seed_ids.add(row.subject_id)
            if row.object_id is not None:
                seed_ids.add(row.object_id)
        if not seed_ids:
            return []
        expanded = await ppr_expand(self.session, list(seed_ids), top_n=settings.graph_facts_k)
        if not expanded:
            return []
        rows = await self.session.execute(
            select(
                FactContent.statement,
                FactContent.predicate,
                FactClaim.valid,
                distance.label("distance"),
            )
            .join(FactClaim, FactClaim.content_id == FactContent.id)
            .where(
                FactContent.embedding.is_not(None),
                or_(FactContent.subject_id.in_(expanded), FactContent.object_id.in_(expanded)),
                *gate,
            )
            .order_by(distance)
            .limit(self.k)
            .execution_options(**opts)
        )
        return fact_hits(rows, margin=settings.ppr_margin)

    async def assemble_context(self) -> tuple[list[Hit], list[FactHit]]:
        """Build one recall round, the fused hits and the merged seed, neighbor, and ppr facts.

        The live default round reads hits, seeds, and neighbors off one hybrid_recall call; a
        replay round keeps its seeds and neighbors on the as_of-aware ORM path instead, since
        hybrid_recall only ever widens the live graph. Multi-hop facts fold in when ppr is on, and
        chunk hits are reranked last against the widened pool hybrid_recall already fetched.
        """
        hits, seeds, neighbors = await self.hybrid_recall()
        if self.as_of is not None:
            seeds = await self.latest_facts()
            neighbors = await self.neighbor_facts()
        multihop = await self.ppr_facts() if self.ppr else []
        if settings.rerank:
            hits = await rerank_hits(self.query, hits, self.k)
        return hits, merge_facts(seeds, neighbors + multihop)

    async def fill_gap(
        self, hits: list[Hit], facts: list[FactHit]
    ) -> tuple[list[Hit], list[FactHit]]:
        """Run one extra targeted round when the first recall left a gap, then merge it in.

        Expands the query with the best evidence already found, re-embeds it, assembles the hit
        and fact lanes through a second Recall bound to the expanded query and vector on the
        same shared session, and merges only the new items so a thin first recall widens once.
        Bounded to this single round so latency stays sane, the EviMem-style re-retrieval.

        hits: the hits the first round recalled.
        facts: the facts the first round recalled.
        """
        expanded_query = expand_query(self.query, hits, facts)
        [vector] = await self.embedder.embed([expanded_query], mode="query")
        expanded_round = Recall(
            self.session, self.embedder, expanded_query, vector, self.k, self.as_of, self.ppr
        )
        more_hits, more_facts = await expanded_round.assemble_context()
        merged_hits = merge_hits(hits, more_hits, settings.rerank_candidates)
        merged_facts = merge_facts(facts, more_facts)
        logger.info(
            "gap fill for {query!r} added {h} hits and {f} facts",
            query=self.query,
            h=len(merged_hits) - len(hits),
            f=len(merged_facts) - len(facts),
        )
        return merged_hits, merged_facts


async def recall(
    query: str,
    principal_id: uuid.UUID | None = None,
    k: int = 8,
    as_of: datetime | None = None,
    scope: uuid.UUID | None = None,
) -> RecallResult:
    """Recall the fused chunk and graph context for a query, the agent's one retrieval entrypoint.

    Opens one `Recall` round for the reranked hits and merged seed, neighbor, and pagerank facts,
    issuing one extra targeted round if recall_gap_fill is on and the context comes back thin, and
    folds in community summaries for a thematic query. When routing is on, the query's route
    narrows the fixed ppr and raptor mix to the route's own lanes, resolved into plain locals
    rather than mutating `settings`.

    query: natural-language query to recall context for.
    principal_id: identity whose row level security visibility scopes the recall, the system
        principal when null.
    k: number of fused hits and of seed facts to surface.
    as_of: world-time the graph facts must be valid at, the live graph when null.
    scope: group id narrowing every lane's read to that group's composed graph, the whole visible
        union when null.
    """
    principal_id = principal_id or settings.system_principal_id
    embedder = Embedder()
    [vector] = await embedder.embed([query], mode="query")
    async with acting_as(principal_id, scope) as session:
        if settings.query_routing:
            plan = QueryRoute.plan(query)
            thematic = plan.communities
            ppr_on = plan.ppr
            raptor_on = plan.raptor
        else:
            thematic = QueryRoute.is_thematic(query)
            ppr_on = settings.ppr
            raptor_on = settings.raptor
        round_ = Recall(session, embedder, query, vector, k, as_of, ppr_on)
        hits, facts = await round_.assemble_context()
        if settings.recall_gap_fill and await has_evidence_gap(query, hits, facts):
            hits, facts = await round_.fill_gap(hits, facts)
        session_notes = (
            await round_.session_hits(settings.session_recall_k)
            if settings.session_recall_k
            else []
        )
        await FactClaim.record_access(session, [fact.statement for fact in facts])
        communities = (
            [
                CommunityNote(label=label, summary=summary, score=score)
                for label, summary, score in await community_search(
                    query, principal_id, scope=scope, k=3
                )
            ]
            if thematic
            else []
        )
        # thematic picks the root-level summaries for a broad query and leaf summaries for a
        # pointed one.
        raptor = (
            [
                RaptorNote(label=label, summary=summary, level=level, score=score)
                for label, summary, level, score in await raptor_search(
                    query, principal_id, thematic=thematic, k=settings.raptor_k, scope=scope
                )
            ]
            if raptor_on
            else []
        )
        profile = await round_.top_profile() if settings.profiles else None
    logger.info(
        "recall {query!r} bundled {hits} hits, {facts} facts, {comms} comms, {raptor} raptor",
        query=query,
        hits=len(hits),
        facts=len(facts),
        comms=len(communities),
        raptor=len(raptor),
    )
    return RecallResult(
        query=query,
        hits=hits,
        facts=facts,
        communities=communities,
        raptor=raptor,
        session=session_notes,
        profile=profile,
        as_of=as_of,
    )
