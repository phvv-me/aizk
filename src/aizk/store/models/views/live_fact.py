from collections.abc import Collection
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self

from patos import sql
from patos.sql import Column as C
from pydantic import UUID5, UUID7, JsonValue
from sqlalchemy import (
    ColumnElement,
    Float,
    Integer,
    and_,
    bindparam,
    case,
    extract,
    func,
    literal,
    type_coerce,
    union,
    union_all,
)
from sqlalchemy.sql.selectable import CTE
from sqlalchemy.sql.selectable import Select as SelectStatement
from sqlmodel import select
from sqlmodel.sql.expression import Select, SelectOfScalar

from ...mixins import ViewBase
from ...vector import cosine_distance
from ..tables.fact import FactClaim, FactContent

if TYPE_CHECKING:
    from ....retrieval.models.lane import QueryContext


def half_life_decay(
    age_days: ColumnElement[float], half_life_days: ColumnElement[float]
) -> ColumnElement[float]:
    """Exponential forgetting-curve retention, one half per half-life: 0.5 ** (age / T)."""
    return func.power(0.5, age_days / half_life_days)


def log_frequency(access_count: ColumnElement[int]) -> ColumnElement[float]:
    """Diminishing-returns access signal, ln(1 + count)."""
    return func.ln(literal(1.0, Float) + access_count.cast(Float))


class LiveFact(ViewBase):
    """Security-invoker view joining current fact claims with immutable content.

    The view earns its migration over a per-statement CTE. One definition serves the ORM
    entity, the raw recall SQL, and psql alike, and since it is a plain security-invoker
    relation the planner inlines it into every calling statement exactly as a CTE would
    while row security still runs as the caller. A CTE would need no migration but would be
    retyped in each statement, drift between the Python and SQL copies, and stay invisible
    from psql. Keep the view.
    """

    id: C[UUID7]
    content_id: C[UUID5]
    subject_id: C[UUID5]
    object_id: C[UUID5 | None]
    predicate: C[str]
    statement: C[str]
    embedding: C[list[float] | None]
    created_by: C[UUID5]
    scopes: C[list[UUID5]]
    valid_from: C[datetime | None]
    valid_to: C[datetime | None]
    recorded_from: C[datetime]
    recorded_to: C[datetime | None]
    last_accessed: C[datetime | None]
    access_count: C[int]
    attributes: C[dict[str, JsonValue]]
    perspective_key: C[str]
    source_chunk_id: C[UUID7 | None]
    promoted_from: C[UUID7 | None]

    @classmethod
    def line(cls) -> ColumnElement[str]:
        """The fact's prompt-ready evidence line, speaker attribution then the predicate
        and statement. A world fact with no recorded speaker skips the attribution
        bracket entirely."""
        speaker_label = cls.attributes >> "speaker_label"
        speaker_name = func.coalesce(speaker_label, "unknown speaker")
        speaker_role = cls.attributes >> "speaker_role"
        speaker_suffix = sql.fragment(t", {speaker_role}")
        epistemic_kind = func.coalesce(cls.attributes >> "epistemic_kind", "world")
        attribution = case(
            (and_(epistemic_kind == "world", speaker_label.is_(None)), ""),
            else_=sql.concat(t"[{speaker_name}{speaker_suffix}, {epistemic_kind}] "),
        )
        predicate, statement = cls.predicate, cls.statement
        return sql.concat(t"- {attribution}({predicate}) {statement}")

    @classmethod
    def embedded(cls) -> SelectOfScalar[Self]:
        """Every live fact carrying an embedding, the corpus graph passes cluster over."""
        return select(cls).where(cls.embedding.is_not(None))

    @classmethod
    def touching(cls, entity_ids: Collection[UUID5], limit: int) -> Select[tuple[UUID5, str]]:
        """The newest bounded fact statements for every named entity.

        entity_ids: the entities whose surrounding facts to load.
        limit: maximum statements retained for each entity profile.
        """
        touches = union_all(
            select(
                cls.subject_id.label("entity_id"),
                cls.statement,
                cls.recorded_from.label("recorded_at"),
                cls.id,
            ).where(cls.subject_id.in_(entity_ids)),
            select(
                cls.object_id.label("entity_id"),
                cls.statement,
                cls.recorded_from.label("recorded_at"),
                cls.id,
            ).where(
                cls.object_id.in_(entity_ids),
                cls.object_id != cls.subject_id,
            ),
        ).subquery("profile_fact_touch")
        ranked = (
            select(
                touches.c.entity_id,
                touches.c.statement,
                touches.c.recorded_at,
                touches.c.id,
            )
            .add_columns(
                func.row_number()
                .over(
                    partition_by=touches.c.entity_id,
                    order_by=(touches.c.recorded_at.desc(), touches.c.id.desc()),
                )
                .label("profile_rank"),
            )
            .subquery("profile_fact_rank")
        )
        return (
            select(ranked.c.entity_id, ranked.c.statement)
            .where(ranked.c.profile_rank <= limit)
            .order_by(ranked.c.entity_id, ranked.c.recorded_at, ranked.c.id)
        )

    @classmethod
    def newest_statements(cls, limit: int) -> SelectOfScalar[str]:
        """The most recently recorded live statements, newest first. Callers chain their
        own predicate filters onto the returned select.

        limit: how many statements to keep.
        """
        return select(cls.statement).order_by(cls.recorded_from.desc()).limit(limit)

    @classmethod
    def dense(cls, context: QueryContext) -> CTE:
        """Dense fact seeds under the floor, ordered by distance with access recency and
        frequency blended in.

        The materialized content cut isolates the vector index scan; live_fact then
        supplies visibility and access history in one join.
        """
        fact_distance = cosine_distance(FactContent.embedding, context.vector)
        dense_fact_content = (
            select(
                FactContent.id,
                FactContent.subject_id,
                FactContent.object_id,
                fact_distance.label("distance"),
            )
            .where(FactContent.embedding.is_not(None), fact_distance < context.floor)
            .order_by(fact_distance)
            .limit(context.fusion_depth)
            .cte("dense_fact_content")
            # prefix_with is SQLAlchemy's supported spelling for a MATERIALIZED CTE.
            .prefix_with("MATERIALIZED")
        )
        last_seen = cls.last_accessed.coalesce(
            cls.recorded_from,
        )
        blended = (
            dense_fact_content.c.distance
            - bindparam("recall_recency_weight", type_=Float)
            * half_life_decay(
                type_coerce(
                    extract("epoch", func.now() - last_seen) / 86_400.0,
                    Float,
                ),
                bindparam("recall_recency_half_life_days", type_=Float),
            )
            - bindparam("recall_frequency_weight", type_=Float) * log_frequency(cls.access_count)
        )
        return (
            select(
                cls.id,
                dense_fact_content.c.subject_id,
                dense_fact_content.c.object_id,
                dense_fact_content.c.distance,
            )
            .add_columns(blended.label("blended"))
            .join(dense_fact_content, dense_fact_content.c.id == cls.content_id)
            .order_by(blended)
            .limit(context.k)
            .cte("dense_fact")
        )

    @staticmethod
    def endpoints(
        dense_facts: CTE, *extra: ColumnElement[Any]
    ) -> tuple[Select[Any] | SelectOfScalar[Any], Select[Any] | SelectOfScalar[Any]]:
        """One select per dense-fact endpoint, the object side guarded against nulls."""
        return (
            select(dense_facts.c.subject_id.label("entity_id"), *extra),
            select(dense_facts.c.object_id.label("entity_id"), *extra).where(
                dense_facts.c.object_id.is_not(None)
            ),
        )

    @classmethod
    def neighbors(cls, dense_facts: CTE, context: QueryContext) -> SelectStatement[Any]:
        """One-hop graph neighbors of the dense seeds as one fact part, ranked by
        distance.

        Each endpoint side joins the seeds through its own index; an OR across both
        endpoints would fall back to scanning every fact.
        """
        seed_entities = union(*cls.endpoints(dense_facts)).cte("seed_entity")
        live_distance = cosine_distance(cls.embedding, context.vector)
        neighbor_sides = [
            select(cls.id, live_distance.label("ordering"))
            .join(seed_entities, endpoint == seed_entities.c.entity_id)
            .where(
                cls.embedding.is_not(None),
                cls.id.not_in(select(dense_facts.c.id)),
            )
            for endpoint in (cls.subject_id, cls.object_id)
        ]
        neighbor_touch = union(*neighbor_sides).subquery("neighbor_touch")
        return (
            select(neighbor_touch.c.id, neighbor_touch.c.ordering)
            .order_by(neighbor_touch.c.ordering)
            .limit(context.k)
        )

    @classmethod
    def diffused(cls, seeds: CTE, ppr_hops: int) -> CTE:
        """The seed mass diffused one bounded degree-normalized hop at a time,
        accumulated over every hop and cut to the mass window.

        Each direction joins the frontier through its own endpoint index instead of a
        membership test over every fact.
        """
        ppr_frontier = bindparam("graph_ppr_frontier", type_=Integer)
        ppr_damping = bindparam("graph_ppr_damping", type_=Float)
        spread = [seeds]
        previous = seeds
        for hop in range(1, ppr_hops + 1):
            frontier = (
                select(previous.c.entity_id, previous.c.mass)
                .order_by(previous.c.mass.desc())
                .limit(ppr_frontier)
                .cte(f"frontier_{hop}")
            )
            edges = union_all(
                select(cls.subject_id.label("src"), cls.object_id.label("dst"))
                .join(frontier, cls.subject_id == frontier.c.entity_id)
                .where(cls.object_id.is_not(None)),
                select(cls.object_id, cls.subject_id).join(
                    frontier, cls.object_id == frontier.c.entity_id
                ),
            ).cte(f"edge_{hop}")
            degree = (
                select(edges.c.src, func.count().label("edges"))
                .group_by(edges.c.src)
                .subquery(f"degree_{hop}")
            )
            degree_scale = func.greatest(degree.c.edges.cast(Float), literal(1.0, Float))
            flow = func.sum((frontier.c.mass * ppr_damping).op("/")(degree_scale))
            previous = (
                select(edges.c.dst.label("entity_id"), flow.label("mass"))
                .select_from(
                    edges.join(frontier, frontier.c.entity_id == edges.c.src).join(
                        degree, degree.c.src == edges.c.src
                    )
                )
                .group_by(edges.c.dst)
                .cte(f"hop_{hop}")
            )
            spread.append(previous)
        accumulated = union_all(
            *(select(step.c.entity_id, step.c.mass) for step in spread)
        ).subquery("spread")
        return (
            select(accumulated.c.entity_id, func.sum(accumulated.c.mass).label("mass"))
            .group_by(accumulated.c.entity_id)
            .order_by(func.sum(accumulated.c.mass).desc())
            .limit(bindparam("graph_mass_window", type_=Integer))
            .cte("entity_mass")
        )

    @classmethod
    def connected(cls, mass: CTE) -> SelectStatement[Any]:
        """The facts the accumulated mass connects, ordered by the weaker endpoint's
        mass.

        A connecting fact needs standing at both endpoints, so the score takes the
        weaker endpoint's mass, which lets a semantically distant hop outrank dense
        near-duplicates that merely touch one popular entity. Semantic order needs no
        second vote here, the dense part of the merged lane already casts it.
        """
        subject_mass = mass.alias("subject_mass")
        object_mass = mass.alias("object_mass")
        connection = func.least(
            subject_mass.c.mass,
            func.coalesce(
                object_mass.c.mass,
                subject_mass.c.mass * bindparam("graph_dangling_factor", type_=Float),
            ),
        )
        return (
            select(cls.id, (-connection).label("ordering"))
            .join(subject_mass, subject_mass.c.entity_id == cls.subject_id)
            .outerjoin(object_mass, object_mass.c.entity_id == cls.object_id)
            .where(cls.embedding.is_not(None))
            .order_by(connection.desc())
            .limit(bindparam("graph_facts_k", type_=Integer))
        )

    @classmethod
    def __view_select__(cls) -> SelectStatement[Any]:
        return (
            select(
                FactClaim.id.label("id"),
                FactClaim.content_id.label("content_id"),
                FactContent.subject_id.label("subject_id"),
                FactContent.object_id.label("object_id"),
            )
            .add_columns(
                FactContent.predicate.label("predicate"),
                FactContent.statement.label("statement"),
                FactContent.embedding.label("embedding"),
                FactClaim.created_by.label("created_by"),
                FactClaim.scopes.label("scopes"),
                FactClaim.valid_from.label("valid_from"),
                FactClaim.valid_to.label("valid_to"),
                FactClaim.recorded_from.label("recorded_from"),
                FactClaim.recorded_to.label("recorded_to"),
                FactClaim.last_accessed.label("last_accessed"),
                FactClaim.access_count.label("access_count"),
                FactClaim.attributes.label("attributes"),
                FactClaim.perspective_key.label("perspective_key"),
                FactClaim.source_chunk_id.label("source_chunk_id"),
                FactClaim.promoted_from.label("promoted_from"),
            )
            .select_from(
                FactClaim.__table__.join(
                    FactContent.__table__,
                    FactContent.id == FactClaim.content_id,
                )
            )
            .where(FactClaim.is_current)
        )
