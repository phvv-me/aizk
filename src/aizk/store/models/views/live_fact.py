import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Self

import sqlalchemy
from sqlalchemy import ColumnElement, and_, case, func, or_
from sqlalchemy.dialects.postgresql import Range
from sqlmodel import select
from sqlmodel.sql.expression import Select, SelectOfScalar

from ....common import sql
from ....common.sql import Column
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

    id: Column[uuid.UUID]
    content_id: Column[uuid.UUID]
    subject_id: Column[uuid.UUID]
    object_id: Column[uuid.UUID | None]
    predicate: Column[str]
    statement: Column[str]
    embedding: Column[list[float] | None]
    created_by: Column[uuid.UUID]
    scopes: Column[list[uuid.UUID]]
    valid: Column[Range[datetime] | None]
    recorded: Column[Range[datetime]]
    last_accessed: Column[datetime | None]
    access_count: Column[int]
    attributes: Column[dict]
    perspective_key: Column[str]
    source_chunk_id: Column[uuid.UUID | None]
    promoted_from: Column[uuid.UUID | None]

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
    def touching(
        cls, entity_ids: Collection[uuid.UUID]
    ) -> Select[tuple[uuid.UUID, uuid.UUID | None, str]]:
        """Subject, object, and statement of every live fact naming one of the given
        entities as subject or object, oldest recorded first with the id as tiebreak.

        entity_ids: the entities whose surrounding facts to load.
        """
        return (
            select(cls.subject_id, cls.object_id, cls.statement)
            .where(
                or_(
                    cls.subject_id.in_(entity_ids),
                    cls.object_id.in_(entity_ids),
                )
            )
            .order_by(func.lower(cls.recorded), cls.id)
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
