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
    and_,
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
from sqlalchemy.sql.selectable import CTE, Subquery
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
    def projectable(cls) -> ColumnElement[bool]:
        """Whether this chunk's source may enter the graph at all.

        A cached page is quarantined and its chunks are never projected, so counting them as
        backlog would show an operator a queue that no pass will ever drain.
        """
        # The runtime import breaks the cycle with Document, which imports Chunk for
        # its ordered-chunks relationship.
        from .document import Document

        return cls.document_id.in_(select(Document.id).where(Document.projectable()))

    @classmethod
    def processing_counts(
        cls, one_hour_ago: datetime, six_hours_ago: datetime, day_ago: datetime
    ) -> Select[tuple[int, int, int, int]]:
        """Caller-visible graph backlog and recent chunk completions in one row."""
        return select(
            cls.id.count().filter(cls.processed_at.is_(None), cls.projectable()).label("queued"),
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
    def ranking(
        cls,
        context: QueryContext,
        ordering: ColumnElement[float],
        *guards: ColumnElement[bool],
        name: str,
        sources: ColumnElement[bool],
    ) -> Subquery:
        """One chunk ranking cut at `fusion_depth`, ordered by `ordering` under `guards`.

        Ranking chunks joined to their documents costs the planner both chunk indexes, since
        under row security it abandons the ANN and bm25 walks, loops over every visible chunk
        and sorts the lot, which on a 22,000-chunk snapshot was 300 of the 320 ms the fused
        statement took and left the two largest indexes in the database written on every
        ingest and read by nothing. So the ranking runs over `chunk` alone inside a
        materialized window the planner cannot fold back into the join, and `document` joins
        outside it to admit only live sources. Row security already hides every chunk whose
        document the caller cannot read, so the window sees exactly what the join would have
        seen, and it reaches `fusion_window` deep so the rows that later join discards are
        paid for out of slack rather than out of the lane.

        An `owned` query is the exception. Its exact scope set is selective, so the planner
        drives the ranking from the few matching documents on its own and a global window
        would spend its over-fetch on documents the selection can never carry and hand back a
        short lane, which is the very thing the scope predicate sits inside the ranking to
        prevent.
        """
        # The runtime import breaks the cycle with Document, which imports Chunk for
        # its ordered-chunks relationship.
        from .document import Document

        if context.owned:
            return (
                select(cls.id, cls.document_id, ordering.label("ordering"))
                .join(Document, Document.id == cls.document_id)
                .where(sources, *guards)
                .order_by(ordering)
                .limit(context.fusion_depth)
                .subquery(f"{name}_ranked")
            )
        window = (
            select(cls.id, cls.document_id, ordering.label("ordering"))
            .where(*guards)
            .order_by(ordering)
            .limit(context.fusion_window)
            .cte(f"{name}_window")
            # prefix_with is SQLAlchemy's supported spelling for a MATERIALIZED CTE.
            .prefix_with("MATERIALIZED")
        )
        return (
            select(window.c.id, window.c.document_id, window.c.ordering)
            .join(Document, Document.id == window.c.document_id)
            .where(sources)
            .order_by(window.c.ordering)
            .limit(context.fusion_depth)
            .subquery(f"{name}_ranked")
        )

    @classmethod
    def fused(cls, context: QueryContext) -> CTE:
        """Fuse dense, lexical, and exact document-title chunk rankings.

        An `owned` query narrows every ranking to one exact scope set before each takes its
        own cut. The predicate belongs here rather than above the union because a caller
        choosing what to share must not have its selection spent by documents it could never
        carry, and a ranking that filtered after cutting would let those documents crowd the
        eligible ones out of the lane.
        """
        # The runtime import breaks the cycle with Document, which imports Chunk for
        # its ordered-chunks relationship.
        from .document import Document

        chunk_distance = cosine_distance(cls.embedding, context.vector)
        active = Document.is_active()
        if context.owned:
            active = and_(active, Document.scopes == context.scope_set)
        dense_ranked = cls.ranking(
            context,
            chunk_distance,
            cls.embedding.is_not(None),
            chunk_distance < context.floor,
            name="dense",
            sources=active,
        )
        dense_chunks = select(
            dense_ranked.c.id,
            dense_ranked.c.document_id,
            func.row_number().over(order_by=dense_ranked.c.ordering).label("rank"),
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
        lexical_ranked = cls.ranking(
            context, text_rank, text_guard, name="lexical", sources=active
        )
        lexical_chunks = (
            select(
                lexical_ranked.c.id,
                lexical_ranked.c.document_id,
                func.row_number().over(order_by=lexical_ranked.c.ordering).label("rank"),
            )
            .where(lexical_ranked.c.ordering < 0)
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
            .where(active, Document.named_in_query())
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
                Document.created_at.label("document_created_at"),
                Document.named_in_query().label("direct"),
                (Document.origin == Document.Origin.web_cache).label("web_cache"),
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
                chunk_scored.c.document_created_at,
                chunk_scored.c.direct,
                chunk_scored.c.web_cache,
                chunk_scored.c.expires_at.label("document_expires_at"),
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
