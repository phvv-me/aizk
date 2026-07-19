from typing import cast

from pydantic import UUID7
from sqlalchemy import Integer, bindparam, func, union_all
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Select
from sqlmodel import select

from ...store import Chunk, Document, Entity, Fact
from ..models.lane import Lane, LaneSelect, QueryContext


class FactLane(Lane):
    """The merged fact lane: ranked parts interleave and the cut hydrates from live_fact.

    The dense seeds, their one-hop graph neighbors, and, past zero hops, the
    personalized PageRank expansion each rank by their own signal, so the merged
    candidate set interleaves parts by rank instead of letting raw cosine distance
    suppress the graph-only evidence.
    """

    kind: Lane.Kind = Lane.Kind.FACTS
    hops: int = 0

    def __call__(self, context: QueryContext) -> LaneSelect:
        """The fact candidates: dense seeds, neighbors, and the optional graph walk.

        The walk is the personalized PageRank expansion: mention-seeded mass diffuses a
        bounded number of degree-normalized hops and the weaker endpoint's accumulated
        mass orders each connecting fact.
        """
        dense_facts = Fact.Live.dense(context)
        seed_part = cast(
            "Select[tuple[UUID7, float]]",
            select(dense_facts.c.id, dense_facts.c.blended.label("ordering")),
        )
        parts: list[Select[tuple[UUID7, float]]] = [
            seed_part,
            Fact.Live.neighbors(dense_facts, context),
        ]
        if self.hops:
            seeds = Entity.seed_mass(dense_facts, context)
            parts.append(Fact.Live.connected(Fact.Live.diffused(seeds, self.hops)))
        return self.merged(parts, context)

    def merged(
        self, parts: list[Select[tuple[UUID7, float]]], context: QueryContext
    ) -> LaneSelect:
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
                ranked_candidates.c.id,
                ranked_candidates.c.rank,
                func.row_number()
                .over(
                    partition_by=(
                        Fact.Live.perspective_key,
                        Fact.Live.statement.lower(),
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
                artifact_id=fact_document.artifact_id,
                artifact_content_id=fact_document.artifact_content_id,
                created_by=Fact.Live.created_by,
            )
            .select_from(fact_candidates)
            .join(Fact.Live, Fact.Live.id == fact_candidates.c.id)
            .outerjoin(fact_source, fact_source.id == Fact.Live.source_chunk_id)
            .outerjoin(fact_document, fact_document.id == fact_source.document_id)
        )
