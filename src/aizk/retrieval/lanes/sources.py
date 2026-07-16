from patos import sql
from sqlalchemy import (
    ColumnElement,
    Float,
    Integer,
    Text,
    bindparam,
    case,
    column,
    func,
    literal,
    or_,
    select,
    type_coerce,
    union_all,
)
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.sql.selectable import CTE, Select

from ...store import Chunk, Document, Entity, Fact, Relation
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
            scopes=hits.c.scopes,
            source_chunk_id=hits.c.id,
            source_title=hits.c.document_title,
            source_uri=hits.c.source_uri,
            created_by=hits.c.created_by,
            direct=hits.c.direct,
        ).select_from(hits)


class EntityCatalogLane(Lane):
    """Live ontology entities grouped by type and exact scope set."""

    kind: Lane.Kind = Lane.Kind.SOURCES

    def __call__(self, context: QueryContext) -> Select:
        """Render query-relevant entity kinds and their current state facts."""
        catalog = entity_catalog(context)
        return self.row(
            evidence_id=catalog.c.id,
            ordering=catalog.c.distance,
            line=sql.concat(t"Current {catalog.c.type} entities are {catalog.c.entries}."),
            scopes=catalog.c.scopes,
            source_title=sql.concat(t"{catalog.c.type} catalog"),
            created_by=catalog.c.created_by,
        ).select_from(catalog)


def entity_catalog(context: QueryContext) -> CTE:
    """Group live entities and their state facts by ontology type and exact scopes."""
    kind_distance = Entity.Kind.embedding @ context.vector
    relevant_kinds = (
        select(Entity.Kind.name, kind_distance.label("distance"))
        .where(Entity.Kind.structural.is_(False), Entity.Kind.embedding.is_not(None))
        .order_by(kind_distance)
        .limit(context.k)
        .cte("relevant_entity_kind")
    )
    declared = (
        select(
            Entity.Content.id,
            Entity.Content.name,
            Entity.Content.type,
            Entity.Claim.scopes,
            Entity.Claim.created_by,
        )
        .join(Entity.Claim, Entity.Claim.content_id == Entity.Content.id)
        .join(
            Document,
            (Document.subject_type == Entity.Content.type)
            & (func.lower(Document.title) == func.lower(Entity.Content.name))
            & (Document.scopes == Entity.Claim.scopes),
        )
        .where(or_(Document.expires_at.is_(None), Document.expires_at > func.now()))
    )
    endpoints = union_all(
        select(
            Fact.Live.subject_id.label("id"),
            Fact.Live.scopes,
            Fact.Live.created_by,
        ),
        select(
            Fact.Live.object_id.label("id"),
            Fact.Live.scopes,
            Fact.Live.created_by,
        ).where(Fact.Live.object_id.is_not(None)),
    ).cte("live_fact_endpoint")
    inferred = select(
        Entity.Content.id,
        Entity.Content.name,
        Entity.Content.type,
        endpoints.c.scopes,
        endpoints.c.created_by,
    ).join(endpoints, endpoints.c.id == Entity.Content.id)
    live = union_all(declared, inferred).cte("live_entity")
    unique = (
        select(live)
        .ext(distinct_on(live.c.type, live.c.name, live.c.scopes))
        .order_by(live.c.type, live.c.name, live.c.scopes, live.c.id)
        .cte("unique_live_entity")
    )
    states = (
        select(
            Fact.Live.subject_id,
            Fact.Live.scopes,
            func.string_agg(Fact.Live.statement, literal(" and ")).label("states"),
        )
        .join(Relation.Kind, Relation.Kind.name == Fact.Live.predicate)
        .where(Relation.Kind.policy == Relation.Policy.state)
        .group_by(Fact.Live.subject_id, Fact.Live.scopes)
        .cte("entity_state")
    )
    entry = unique.c.name + func.coalesce(literal(" (") + states.c.states + literal(")"), "")
    rows = (
        select(
            unique.c.id,
            unique.c.created_by,
            unique.c.scopes,
            unique.c.type,
            relevant_kinds.c.distance,
            func.row_number()
            .over(
                partition_by=(unique.c.type, unique.c.scopes),
                order_by=unique.c.name,
            )
            .label("rank"),
            func.string_agg(entry, literal(", "))
            .over(
                partition_by=(unique.c.type, unique.c.scopes),
                order_by=unique.c.name,
                rows=(None, None),
            )
            .label("entries"),
        )
        .join(relevant_kinds, relevant_kinds.c.name == unique.c.type)
        .outerjoin(
            states,
            (states.c.subject_id == unique.c.id) & (states.c.scopes == unique.c.scopes),
        )
        .cte("entity_catalog_row")
    )
    return select(rows).where(rows.c.rank == 1).cte("entity_catalog")


def reciprocal_rank_fusion(rank: ColumnElement[int]) -> ColumnElement[float]:
    """One ranking's reciprocal-rank-fusion vote, 1 / (k + rank), after Cormack et al."""
    # type_coerce renders no SQL; it only pins the Python-side type the untyped
    # `rrf_k` bind would otherwise leave unknown.
    return type_coerce(1.0 / (bindparam("rrf_k") + rank), Float)


def fused_chunks(context: QueryContext) -> CTE:
    """Fuse dense, lexical, and exact document-title chunk rankings."""
    chunk_distance = Chunk.embedding @ context.vector
    active = source_is_active()
    dense_ranked = (
        select(Chunk.id, Chunk.document_id, chunk_distance.label("distance"))
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.embedding.is_not(None), chunk_distance < context.floor, active)
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
        .join(Document, Document.id == Chunk.document_id)
        .where(active)
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
            Chunk.id,
            Chunk.document_id,
            func.row_number()
            .over(order_by=(func.length(Document.title).desc(), Chunk.ord))
            .label("rank"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(source_is_active(), source_title_matches())
        .order_by(func.length(Document.title).desc(), Chunk.ord)
        .limit(context.fusion_depth)
        .cte("title_chunk")
    )

    chunk_lanes = union_all(
        select(dense_chunks), select(lexical_chunks), select(title_chunks)
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


def source_is_active() -> ColumnElement[bool]:
    """Whether a source chunk has no expiry or remains valid at database time."""
    return or_(Document.expires_at.is_(None), Document.expires_at > func.now())


def source_title_matches() -> ColumnElement[bool]:
    """Whether the query contains the source's complete title."""
    query = func.lower(bindparam("qtext", type_=Text))
    return func.strpos(query, func.lower(Document.title)) > 0


def hybrid_chunks(context: QueryContext) -> CTE:
    """The capped hybrid chunk cut: fused ranks scored with the promoted bonus, at most
    `recall_per_document` hits per document, `k` hits in total."""
    fused = fused_chunks(context)
    promoted = Document.promoted_from.is_not(None)
    source_score = (
        fused.c.rrf_score
        + case((promoted, bindparam("promoted_bonus", type_=Float)), else_=0.0)
        + case((source_title_matches(), literal(1.0)), else_=0.0)
    )
    chunk_scored = (
        select(
            fused.c.id,
            fused.c.document_id,
            Document.title.label("document_title"),
            Document.source_uri,
            Chunk.text,
            Chunk.created_by,
            Document.scopes,
            (Chunk.provenance >> "speaker_label").label("speaker_label"),
            (Chunk.provenance >> "speaker_role").label("speaker_role"),
            Document.observed_at,
            Document.expires_at,
            source_title_matches().label("direct"),
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
