import uuid
from typing import ClassVar

from sqlalchemy import Column as SAColumn
from sqlalchemy import ColumnElement, Index, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import declared_attr
from sqlmodel import Field

from ....common import sql
from ....common.sql import Column
from ...mixins import Embedded, Id, Scoped, TableBase, Timestamped


class Community(Id, Scoped, Timestamped, Embedded, TableBase, table=True):
    """Scoped entity cluster summarized for thematic retrieval."""

    deletable: ClassVar[bool] = True

    label: Column[str] = Field(sa_type=Text)
    summary: Column[str] = Field(sa_type=Text)
    member_ids: Column[list[uuid.UUID]] = Field(
        default_factory=list, sa_column=SAColumn(ARRAY(Uuid), nullable=False)
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
