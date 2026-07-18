from enum import auto

import rls
from patos import sql
from patos.sql import Column as C

from ....config import settings
from ...mixins import TableBase, Timestamped


class OntologyKind(sql.Model):
    """Grow-only entity or relation vocabulary entry.

    Deliberately open: the vocabulary is a global catalog shared by every tenant, which
    also means LLM-grown kinds leak names across tenants, a tradeoff recorded in the
    Zettelkasten and revisited before multi-tenancy hardens.
    """

    __rls__ = rls.Open()

    name = sql.PK(str)
    description: C[str]
    domain: C[str]
    structural = sql.Field(bool, default=False)


class EntityKind(OntologyKind, Timestamped, TableBase, table=True):
    """The live catalog of entity types `EntityContent.type` foreign-keys against."""

    embedding = sql.Field(
        list[float] | None,
        default=None,
        sa_type=sql.CosineHalfvec(settings.embed_dim),
    )


class RelationPolicy(sql.PGEnum):
    """How facts under one relation coexist or replace prior values."""

    set = auto()
    state = auto()
    event = auto()


class RelationKind(OntologyKind, Timestamped, TableBase, table=True):
    """The live catalog of relation types `FactContent.predicate` foreign-keys against."""

    policy = sql.Field(
        RelationPolicy,
        default=RelationPolicy.set,
    )
