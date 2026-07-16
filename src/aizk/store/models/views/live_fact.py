from collections.abc import Collection
from datetime import datetime
from typing import Self

import sqlalchemy
from patos import sql
from pydantic import UUID5, UUID7
from sqlalchemy import ColumnElement, and_, case, func, union_all
from sqlalchemy.dialects.postgresql import Range
from sqlmodel import select
from sqlmodel.sql.expression import Select, SelectOfScalar

from ...mixins import ViewBase
from ..tables.fact import FactClaim, FactContent


class LiveFact(ViewBase):
    """Security-invoker view joining current fact claims with immutable content.

    The view earns its migration over a per-statement CTE. One definition serves the ORM
    entity, the raw recall SQL, and psql alike, and since it is a plain security-invoker
    relation the planner inlines it into every calling statement exactly as a CTE would
    while row security still runs as the caller. A CTE would need no migration but would be
    retyped in each statement, drift between the Python and SQL copies, and stay invisible
    from psql. Keep the view.
    """

    id: sql.Column[UUID7]
    content_id: sql.Column[UUID5]
    subject_id: sql.Column[UUID5]
    object_id: sql.Column[UUID5 | None]
    predicate: sql.Column[str]
    statement: sql.Column[str]
    embedding: sql.Column[list[float] | None]
    created_by: sql.Column[UUID5]
    scopes: sql.Column[list[UUID5]]
    valid: sql.Column[Range[datetime] | None]
    recorded: sql.Column[Range[datetime]]
    last_accessed: sql.Column[datetime | None]
    access_count: sql.Column[int]
    attributes: sql.Column[dict]
    perspective_key: sql.Column[str]
    source_chunk_id: sql.Column[UUID7 | None]
    promoted_from: sql.Column[UUID7 | None]

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
                func.lower(cls.recorded).label("recorded_at"),
                cls.id,
            ).where(cls.subject_id.in_(entity_ids)),
            select(
                cls.object_id.label("entity_id"),
                cls.statement,
                func.lower(cls.recorded).label("recorded_at"),
                cls.id,
            ).where(
                cls.object_id.in_(entity_ids),
                cls.object_id != cls.subject_id,
            ),
        ).subquery("profile_fact_touch")
        ranked = sqlalchemy.select(
            touches,
            func.row_number()
            .over(
                partition_by=touches.c.entity_id,
                order_by=(touches.c.recorded_at.desc(), touches.c.id.desc()),
            )
            .label("profile_rank"),
        ).subquery("profile_fact_rank")
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
        return select(cls.statement).order_by(func.lower(cls.recorded).desc()).limit(limit)

    @classmethod
    def __view_select__(cls) -> sqlalchemy.Select:
        return (
            sqlalchemy.select(
                FactClaim.id.label("id"),
                FactClaim.content_id.label("content_id"),
                FactContent.subject_id.label("subject_id"),
                FactContent.object_id.label("object_id"),
                FactContent.predicate.label("predicate"),
                FactContent.statement.label("statement"),
                FactContent.embedding.label("embedding"),
                FactClaim.created_by.label("created_by"),
                FactClaim.scopes.label("scopes"),
                FactClaim.valid.label("valid"),
                FactClaim.recorded.label("recorded"),
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
