from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Self

from patos import sql
from patos.sql import Column as C
from pydantic import UUID7, JsonValue
from sqlalchemy import Column as SAColumn
from sqlalchemy import (
    ColumnElement,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    bindparam,
    case,
    column,
    func,
    literal,
    literal_column,
    true,
    type_coerce,
    union_all,
)
from sqlalchemy.orm import declared_attr
from sqlalchemy.sql.selectable import CTE
from sqlmodel import Field, select
from sqlmodel.sql.expression import Select, SelectOfScalar

from ....config import DatabaseBackend, settings
from ...mixins import Embedded, Id, Scoped, TableBase
from ...vector import cosine_distance

if TYPE_CHECKING:
    from ....retrieval.models.lane import QueryContext


class Chunk(Id, Scoped, Embedded, TableBase, table=True):
    """Store one ordered source span with parent-inherited visibility and retrieval indexes."""

    mutable: ClassVar[bool] = True
    deletable: ClassVar[bool] = True
    read_through: ClassVar[str | None] = "document"

    document_id: C[UUID7] = Field(
        foreign_key="document.id", ondelete="CASCADE", nullable=False, index=True
    )
    ord: C[int]
    text: C[str]
    lexical = sql.Nullable(str)
    tokens = sql.Nullable(int)
    provenance: C[dict[str, JsonValue]] = Field(
        default_factory=dict, sa_type=sql.TypedJSONB, sa_column_kwargs={"server_default": "{}"}
    )
    processed_at: C[datetime | None] = Field(
        default=None, sa_column=SAColumn(DateTime(timezone=True))
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | UniqueConstraint, ...]:
        return (
            *super().__table_args__,
            Index("ix_chunk_scopes", "scopes", postgresql_using="gin"),
            Index(
                "ix_chunk_pending",
                "id",
                postgresql_where=SAColumn("processed_at").is_(None),
            ),
        )

    @classmethod
    def processing_counts(
        cls, one_hour_ago: datetime, six_hours_ago: datetime, day_ago: datetime
    ) -> Select[tuple[int, int, int, int]]:
        """Caller-visible graph backlog and recent chunk completions in one row."""
        return select(
            cls.id.count().filter(cls.processed_at.is_(None)).label("queued"),
            cls.id.count().filter(cls.processed_at >= one_hour_ago).label("completed_1h"),
            cls.id.count().filter(cls.processed_at >= six_hours_ago).label("completed_6h"),
            cls.id.count().filter(cls.processed_at >= day_ago).label("completed_24h"),
        )

    @classmethod
    def at(cls, document_id: UUID7, ordinal: int) -> SelectOfScalar[Self]:
        """The chunk standing at one exact document ordinal."""
        return select(cls).where(
            cls.__table__.c.document_id == document_id,
            cls.__table__.c.ord == ordinal,
        )

    @classmethod
    def fused(cls, context: QueryContext) -> CTE:
        """Fuse dense, lexical, and exact document-title chunk rankings."""
        # The runtime import breaks the cycle with Document, which imports Chunk for
        # its ordered-chunks relationship.
        from .document import Document

        chunk_distance = cosine_distance(cls.embedding, context.vector)
        active = Document.is_active()
        dense_ranked = (
            select(cls.id, cls.document_id, chunk_distance.label("distance"))
            .join(Document, Document.id == cls.document_id)
            .where(cls.embedding.is_not(None), chunk_distance < context.floor, active)
            .order_by(chunk_distance)
            .limit(context.fusion_depth)
            .subquery("dense_ranked")
        )
        dense_chunks = select(
            dense_ranked.c.id,
            dense_ranked.c.document_id,
            func.row_number().over(order_by=dense_ranked.c.distance).label("rank"),
        ).cte("dense_chunk")

        text_rank: ColumnElement[float]
        text_guard: ColumnElement[bool]
        if settings.database_backend is DatabaseBackend.cockroachdb:
            language: ColumnElement[str] = literal_column("'english'")
            searchable = func.to_tsvector(language, func.coalesce(cls.lexical, cls.text))
            text_query = func.plainto_tsquery(language, bindparam("qtext"))
            text_rank = -func.ts_rank(searchable, text_query)
            text_guard = searchable.op("@@")(text_query)
        else:
            # The bm25 column and its index live only in the PostgreSQL migration.
            text_query = func.to_bm25query(
                "ix_chunk_bm25", func.tokenize(bindparam("qtext"), "aizk_bm25")
            )
            text_rank = column("bm25").op("<&>")(text_query)
            text_guard = true()
        lexical_ranked = (
            select(cls.id, cls.document_id, text_rank.label("raw_rank"))
            .join(Document, Document.id == cls.document_id)
            .where(active, text_guard)
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

        title_chunks = (
            select(
                cls.id,
                cls.document_id,
                func.row_number()
                .over(order_by=(Document.title.length().desc(), cls.ord))
                .label("rank"),
            )
            .join(Document, Document.id == cls.document_id)
            .where(Document.is_active(), Document.named_in_query())
            .order_by(Document.title.length().desc(), cls.ord)
            .limit(context.fusion_depth)
            .cte("title_chunk")
        )

        chunk_lanes = union_all(
            select(dense_chunks.c.id, dense_chunks.c.document_id, dense_chunks.c.rank),
            select(lexical_chunks.c.id, lexical_chunks.c.document_id, lexical_chunks.c.rank),
            select(title_chunks.c.id, title_chunks.c.document_id, title_chunks.c.rank),
        ).subquery("chunk_lanes")
        return (
            select(
                chunk_lanes.c.id,
                chunk_lanes.c.document_id,
                func.sum(reciprocal_rank_fusion(chunk_lanes.c.rank)).label("rrf_score"),
            )
            .group_by(chunk_lanes.c.id, chunk_lanes.c.document_id)
            .cte("fused_chunk")
        )

    @classmethod
    def hybrid(cls, context: QueryContext) -> CTE:
        """The capped hybrid chunk cut: fused ranks scored with the promoted bonus, at
        most `recall_per_document` hits per document, `k` hits in total."""
        from .document import Document

        fused = cls.fused(context)
        promoted = Document.promoted_from.is_not(None)
        source_score = (
            fused.c.rrf_score
            + case((promoted, bindparam("promoted_bonus", type_=Float)), else_=0.0)
            + case((Document.named_in_query(), literal(1.0)), else_=0.0)
        )
        chunk_scored = (
            select(
                fused.c.id,
                fused.c.document_id,
                Document.title.label("document_title"),
                Document.source_uri,
            )
            .add_columns(
                Document.artifact_id,
                Document.artifact_content_id,
                cls.text,
                cls.created_by,
                Document.scopes,
                (cls.provenance >> "speaker_label").label("speaker_label"),
                (cls.provenance >> "speaker_role").label("speaker_role"),
                Document.observed_at,
                Document.expires_at,
                Document.named_in_query().label("direct"),
                source_score.label("score"),
                func.row_number()
                .over(partition_by=fused.c.document_id, order_by=source_score.desc())
                .label("document_rank"),
            )
            .join(Document, Document.id == fused.c.document_id)
            .join(cls, cls.id == fused.c.id)
            .subquery("chunk_scored")
        )
        return (
            select(
                chunk_scored.c.id,
                chunk_scored.c.document_id,
                chunk_scored.c.document_title,
                chunk_scored.c.source_uri,
            )
            .add_columns(
                chunk_scored.c.artifact_id,
                chunk_scored.c.artifact_content_id,
                chunk_scored.c.text,
                chunk_scored.c.created_by,
                chunk_scored.c.scopes,
                chunk_scored.c.speaker_label,
                chunk_scored.c.speaker_role,
                chunk_scored.c.observed_at,
                chunk_scored.c.expires_at,
                chunk_scored.c.direct,
                chunk_scored.c.score,
                chunk_scored.c.document_rank,
            )
            .where(chunk_scored.c.document_rank <= bindparam("recall_per_document", type_=Integer))
            .order_by(chunk_scored.c.score.desc())
            .limit(context.k)
            .cte("chunk_capped")
        )

    @staticmethod
    def source_line(hits: CTE) -> ColumnElement[str]:
        """One hit's source by speaker line with its whitespace-flattened snippet.

        The speaker fields read the capped CTE's projected columns rather than the Chunk
        model's, so this rendering stays with the query instead of the table.
        """
        speaker_role = sql.fragment(t" ({hits.c.speaker_role})")
        speaker = sql.fragment(t" by {hits.c.speaker_label}{speaker_role}")
        timezone = bindparam("display_timezone", type_=Text)
        observed_at = func.timezone(timezone, hits.c.observed_at)
        expires_at = func.timezone(timezone, hits.c.expires_at)
        observed = sql.fragment(t" observed {func.to_char(observed_at, 'YYYY-MM-DD')}")
        expires = sql.fragment(t" expires {func.to_char(expires_at, 'YYYY-MM-DD')}")
        source_name = func.coalesce(hits.c.document_title, hits.c.source_uri, "untitled")
        snippet = func.left(
            func.regexp_replace(hits.c.text, r"\s+", " ", "g"),
            bindparam("chunk_size", type_=Integer),
        )
        return sql.concat(t"{source_name}{speaker}{observed}{expires}\n  {snippet}")


def reciprocal_rank_fusion(rank: ColumnElement[int]) -> ColumnElement[float]:
    """One ranking's reciprocal-rank-fusion vote, 1 / (k + rank), after Cormack et al."""
    return type_coerce(
        literal(1.0, Float) / (bindparam("rrf_k", type_=Float) + rank.cast(Float)),
        Float,
    )
