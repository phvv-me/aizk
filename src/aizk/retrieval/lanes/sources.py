from sqlalchemy import (
    ColumnElement,
    Float,
    Integer,
    Numeric,
    bindparam,
    case,
    cast,
    column,
    func,
    select,
    type_coerce,
    union_all,
)
from sqlalchemy.sql.selectable import CTE, Select

from ...common import sql
from ...store import Chunk, Document
from ..models.lane import Lane, QueryContext


class SourceLane(Lane):
    """Hybrid source retrieval as one lane.

    Dense and lexical chunk rankings fuse under the floor, promoted documents earn
    their bonus, hits cap per document, and each kept chunk renders as one scored
    source line.
    """

    kind: Lane.Kind = Lane.Kind.SOURCES

    def __call__(self, context: QueryContext) -> Select:
        """The capped hybrid chunk hits rendered as scored source lines."""
        hits = hybrid_chunks(context)
        return self.row(
            evidence_id=hits.c.id,
            ordering=-hits.c.score,
            line=source_line(hits),
            source_chunk_id=hits.c.id,
            source_title=hits.c.document_title,
            source_uri=hits.c.source_uri,
            created_by=hits.c.created_by,
        ).select_from(hits)


def reciprocal_rank_fusion(rank: ColumnElement[int]) -> ColumnElement[float]:
    """One ranking's reciprocal-rank-fusion vote, 1 / (k + rank), after Cormack et al."""
    # type_coerce renders no SQL; it only pins the Python-side type the untyped
    # `rrf_k` bind would otherwise leave unknown.
    return type_coerce(1.0 / (bindparam("rrf_k") + rank), Float)


def fused_chunks(context: QueryContext) -> CTE:
    """The RRF fusion of one dense and one lexical chunk ranking under the floor."""
    chunk_distance = Chunk.embedding @ context.vector
    dense_ranked = (
        select(Chunk.id, Chunk.document_id, chunk_distance.label("distance"))
        .where(Chunk.embedding.is_not(None), chunk_distance < context.floor)
        .order_by(chunk_distance)
        .limit(context.fusion_depth)
        .subquery("dense_ranked")
    )
    dense_chunks = select(
        dense_ranked.c.id,
        dense_ranked.c.document_id,
        func.row_number().over(order_by=dense_ranked.c.distance).label("rank"),
    ).cte("dense_chunk")

    # The bm25 column and its index live only in the migration, never on the model.
    text_query = func.to_bm25query("ix_chunk_bm25", func.tokenize(bindparam("qtext"), "aizk_bm25"))
    text_rank = column("bm25").op("<&>")(text_query)
    lexical_ranked = (
        select(Chunk.id, Chunk.document_id, text_rank.label("raw_rank"))
        .order_by(text_rank)
        .limit(context.fusion_depth)
        .subquery("lexical_ranked")
    )
    lexical_chunks = (
        select(
            lexical_ranked.c.id,
            lexical_ranked.c.document_id,
            func.row_number().over(order_by=lexical_ranked.c.raw_rank).label("rank"),
        )
        .where(lexical_ranked.c.raw_rank < 0)
        .cte("lexical_chunk")
    )

    chunk_lanes = union_all(select(dense_chunks), select(lexical_chunks)).subquery("chunk_lanes")
    return (
        select(
            chunk_lanes.c.id,
            chunk_lanes.c.document_id,
            func.sum(reciprocal_rank_fusion(chunk_lanes.c.rank)).label("rrf_score"),
        )
        .group_by(chunk_lanes.c.id, chunk_lanes.c.document_id)
        .cte("fused_chunk")
    )


def hybrid_chunks(context: QueryContext) -> CTE:
    """The capped hybrid chunk cut: fused ranks scored with the promoted bonus, at most
    `recall_per_document` hits per document, `k` hits in total."""
    fused = fused_chunks(context)
    promoted = Document.promoted_from.is_not(None)
    source_score = fused.c.rrf_score + case(
        (promoted, bindparam("promoted_bonus", type_=Float)), else_=0.0
    )
    chunk_scored = (
        select(
            fused.c.id,
            fused.c.document_id,
            Document.title.label("document_title"),
            Document.source_uri,
            Chunk.text,
            Chunk.created_by,
            (Chunk.provenance >> "speaker_label").label("speaker_label"),
            (Chunk.provenance >> "speaker_role").label("speaker_role"),
            source_score.label("score"),
            func.row_number()
            .over(partition_by=fused.c.document_id, order_by=source_score.desc())
            .label("document_rank"),
        )
        .join(Document, Document.id == fused.c.document_id)
        .join(Chunk, Chunk.id == fused.c.id)
        .subquery("chunk_scored")
    )
    return (
        select(chunk_scored)
        .where(chunk_scored.c.document_rank <= bindparam("recall_per_document", type_=Integer))
        .order_by(chunk_scored.c.score.desc())
        .limit(context.k)
        .cte("chunk_capped")
    )


def source_line(hits: CTE) -> ColumnElement[str]:
    """One hit's `[score] source by speaker` line with its whitespace-flattened snippet.

    The speaker fields read the capped CTE's projected columns rather than the Chunk
    model's, so this rendering stays with the query instead of the table.
    """
    speaker_role = sql.fragment(t" ({hits.c.speaker_role})")
    speaker = sql.fragment(t" by {hits.c.speaker_label}{speaker_role}")
    # Postgres has no round(double precision, integer), only round(numeric, integer).
    rounded_score = func.round(cast(hits.c.score, Numeric), 3)
    source_name = func.coalesce(hits.c.document_title, hits.c.source_uri, "untitled")
    snippet = func.left(
        func.regexp_replace(hits.c.text, r"\s+", " ", "g"),
        bindparam("snippet_chars", type_=Integer),
    )
    return sql.concat(t"[{rounded_score}] {source_name}{speaker}\n  {snippet}")
