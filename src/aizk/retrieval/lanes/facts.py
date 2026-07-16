from patos import sql
from sqlalchemy import (
    ColumnElement,
    Float,
    Integer,
    bindparam,
    func,
    select,
    union,
    union_all,
)
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import CTE, Select

from ...store import Chunk, Document, Fact
from ..models.lane import Lane, QueryContext
from .graph import endpoint_selects, multihop_part


class FactLane(Lane):
    """The merged fact lane: ranked parts interleave and the cut hydrates from live_fact.

    The dense seeds, their one-hop graph neighbors, and, past zero hops, the
    personalized PageRank expansion each rank by their own signal, so the merged
    candidate set interleaves parts by rank instead of letting raw cosine distance
    suppress the graph-only evidence.
    """

    kind: Lane.Kind = Lane.Kind.FACTS
    hops: int = 0

    def __call__(self, context: QueryContext) -> Select:
        """The fact candidates: dense seeds, neighbors, and the optional graph walk."""
        dense_facts = dense_fact_cte(context)
        parts = [seed_part(dense_facts), neighbor_part(dense_facts, context)]
        if self.hops:
            parts.append(multihop_part(dense_facts, context, self.hops))
        return self.merged(parts, context)

    def merged(self, parts: list[Select], context: QueryContext) -> Select:
        """The parts interleaved by rank, hydrated with attribution and provenance."""
        part_subqueries = [part.subquery(f"fact_part_{index}") for index, part in enumerate(parts)]
        ranked_parts = union_all(
            *(
                select(
                    part.c.id, func.row_number().over(order_by=part.c.ordering).label("part_rank")
                )
                for part in part_subqueries
            )
        ).subquery("fact_parts")
        ranked_candidates = (
            select(ranked_parts.c.id, func.min(ranked_parts.c.part_rank).label("rank"))
            .group_by(ranked_parts.c.id)
            .order_by(func.min(ranked_parts.c.part_rank), ranked_parts.c.id)
            .cte("ranked_fact_candidate")
        )
        statement_candidates = (
            select(
                ranked_candidates,
                func.row_number()
                .over(
                    partition_by=(
                        Fact.Live.perspective_key,
                        func.lower(Fact.Live.statement),
                    ),
                    order_by=(ranked_candidates.c.rank, ranked_candidates.c.id),
                )
                .label("evidence_rank"),
            )
            .join(Fact.Live, Fact.Live.id == ranked_candidates.c.id)
            .cte("statement_fact_candidate")
        )
        fact_candidates = (
            select(statement_candidates.c.id, statement_candidates.c.rank)
            .where(statement_candidates.c.evidence_rank == 1)
            .order_by(statement_candidates.c.rank, statement_candidates.c.id)
            .limit(context.k * bindparam("fact_candidate_factor", type_=Integer))
            .cte("fact_candidate")
        )
        fact_source = aliased(Chunk, name="fact_source")
        fact_document = aliased(Document, name="fact_document")
        return (
            self.row(
                evidence_id=Fact.Live.id,
                ordering=fact_candidates.c.rank,
                line=Fact.Live.line(),
                scopes=Fact.Live.scopes,
                fact_id=Fact.Live.id,
                source_chunk_id=Fact.Live.source_chunk_id,
                source_title=fact_document.title,
                source_uri=fact_document.source_uri,
                created_by=Fact.Live.created_by,
            )
            .select_from(fact_candidates)
            .join(Fact.Live, Fact.Live.id == fact_candidates.c.id)
            .outerjoin(fact_source, fact_source.id == Fact.Live.source_chunk_id)
            .outerjoin(fact_document, fact_document.id == fact_source.document_id)
        )


def half_life_decay(
    age_days: ColumnElement[float], half_life_days: ColumnElement[float]
) -> ColumnElement[float]:
    """Exponential forgetting-curve retention, one half per half-life: 0.5 ** (age / T)."""
    return func.power(0.5, age_days / half_life_days)


def log_frequency(access_count: ColumnElement[int]) -> ColumnElement[float]:
    """Diminishing-returns access signal, ln(1 + count)."""
    return func.ln(1 + access_count)


def dense_fact_cte(context: QueryContext) -> CTE:
    """Dense fact seeds under the floor, ordered by distance with access recency and
    frequency blended in.

    The materialized content cut isolates the vector index scan; live_fact then supplies
    visibility and access history in one join.
    """
    fact_distance = Fact.Content.embedding @ context.vector
    dense_fact_content = (
        select(
            Fact.Content.id,
            Fact.Content.subject_id,
            Fact.Content.object_id,
            fact_distance.label("distance"),
        )
        .where(Fact.Content.embedding.is_not(None), fact_distance < context.floor)
        .order_by(fact_distance)
        .limit(context.fusion_depth)
        .cte("dense_fact_content")
        # prefix_with is SQLAlchemy's supported spelling for a MATERIALIZED CTE.
        .prefix_with("MATERIALIZED")
    )
    last_seen = func.coalesce(Fact.Live.last_accessed, func.lower(Fact.Live.recorded))
    blended = (
        dense_fact_content.c.distance
        - bindparam("recall_recency_weight", type_=Float)
        * half_life_decay(
            sql.days_since(last_seen), bindparam("recall_recency_half_life_days", type_=Float)
        )
        - bindparam("recall_frequency_weight", type_=Float) * log_frequency(Fact.Live.access_count)
    )
    return (
        select(
            Fact.Live.id,
            dense_fact_content.c.subject_id,
            dense_fact_content.c.object_id,
            dense_fact_content.c.distance,
            blended.label("blended"),
        )
        .join(dense_fact_content, dense_fact_content.c.id == Fact.Live.content_id)
        .order_by(blended)
        .limit(context.k)
        .cte("dense_fact")
    )


def seed_part(dense_facts: CTE) -> Select:
    """The dense seeds as one fact part, ranked by their blended order."""
    return select(dense_facts.c.id, dense_facts.c.blended.label("ordering"))


def neighbor_part(dense_facts: CTE, context: QueryContext) -> Select:
    """One-hop graph neighbors of the dense seeds as one fact part, ranked by distance.

    Each endpoint side joins the seeds through its own index; an OR across both endpoints
    would fall back to scanning every fact.
    """
    seed_entities = union(*endpoint_selects(dense_facts)).cte("seed_entity")
    live_distance = Fact.Live.embedding @ context.vector
    neighbor_sides = [
        select(Fact.Live.id, live_distance.label("ordering"))
        .join(seed_entities, endpoint == seed_entities.c.entity_id)
        .where(
            Fact.Live.embedding.is_not(None),
            Fact.Live.id.not_in(select(dense_facts.c.id)),
        )
        for endpoint in (Fact.Live.subject_id, Fact.Live.object_id)
    ]
    neighbor_touch = union(*neighbor_sides).subquery("neighbor_touch")
    return (
        select(neighbor_touch.c.id, neighbor_touch.c.ordering)
        .order_by(neighbor_touch.c.ordering)
        .limit(context.k)
    )
