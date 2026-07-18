from typing import ClassVar

from patos import sql
from pydantic import UUID5
from sqlalchemy import ColumnElement, Index, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import declared_attr

from ...mixins import Embedded, Id, Scoped, TableBase, Timestamped


class Community(Id, Scoped, Timestamped, Embedded, TableBase, table=True):
    """Scoped entity cluster summarized for thematic retrieval."""

    deletable: ClassVar[bool] = True

    label = sql.Field(str)
    summary = sql.Field(str)
    member_ids = sql.Field(
        list[UUID5],
        default_factory=list,
        sa_type=ARRAY(Uuid),
        server_default=text("'{}'::uuid[]"),
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | UniqueConstraint, ...]:
        return (
            *super().__table_args__,
            Index("ix_community_scopes", "scopes", postgresql_using="gin"),
        )

    @classmethod
    def line(cls) -> ColumnElement[str]:
        """The community's `- label: summary` evidence line."""
        label, summary = cls.label, cls.summary
        return sql.concat(t"- {label}: {summary}")
