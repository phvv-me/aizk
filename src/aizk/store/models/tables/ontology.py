from enum import auto
from typing import cast

import rls
from patos import sql
from sqlalchemy import Boolean, Text
from sqlalchemy import Column as SAColumn
from sqlmodel import Field

from ....config import settings
from ...mixins import TableBase, Timestamped


class OntologyKind:
    """Grow-only entity or relation vocabulary entry.

    Deliberately open: the vocabulary is a global catalog shared by every tenant, which
    also means LLM-grown kinds leak names across tenants, a tradeoff recorded in the
    Zettelkasten and revisited before multi-tenancy hardens.
    """

    __rls__ = rls.Open()

    name: sql.Column[str] = Field(sa_type=Text, primary_key=True)
    description: sql.Column[str] = Field(sa_type=Text)
    domain: sql.Column[str] = Field(sa_type=Text)
    structural: sql.Column[bool] = Field(
        default=False, sa_type=Boolean, sa_column_kwargs={"server_default": "false"}
    )


class EntityKind(OntologyKind, Timestamped, TableBase, table=True):
    """The live catalog of entity types `EntityContent.type` foreign-keys against."""

    embedding: sql.Column[list[float] | None] = Field(
        default=None,
        sa_type=cast(type[list[float]], sql.CosineHalfvec(settings.embed_dim)),
    )


class RelationPolicy(sql.PGEnum):
    """How facts under one relation coexist or replace prior values."""

    set = auto()
    state = auto()
    event = auto()


class RelationKind(OntologyKind, Timestamped, TableBase, table=True):
    """The live catalog of relation types `FactContent.predicate` foreign-keys against."""

    policy: sql.Column[RelationPolicy] = Field(
        default=RelationPolicy.set,
        sa_column=SAColumn(
            RelationPolicy.type,
            nullable=False,
            server_default=RelationPolicy.set,
        ),
    )
