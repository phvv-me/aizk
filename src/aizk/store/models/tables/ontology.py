import rls
from inflection import parameterize, underscore
from sqlalchemy import Boolean, Text
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Field, select

from ....common.sql import Column
from ...engine import Session
from ...mixins import TableBase, Timestamped


class OntologyKind:
    """Grow-only entity or relation vocabulary entry.

    Deliberately open: the vocabulary is a global catalog shared by every tenant, which
    also means LLM-grown kinds leak names across tenants, a tradeoff recorded in the
    Zettelkasten and revisited before multi-tenancy hardens.
    """

    __rls__ = rls.Open()

    name: Column[str] = Field(sa_type=Text, primary_key=True)
    description: Column[str] = Field(sa_type=Text)
    domain: Column[str] = Field(sa_type=Text)
    structural: Column[bool] = Field(
        default=False, sa_type=Boolean, sa_column_kwargs={"server_default": "false"}
    )

    @staticmethod
    def canonical(name: str) -> str:
        """Canonicalize vocabulary names to snake case."""
        return parameterize(underscore(name), separator="_")

    @classmethod
    async def define(cls, session: Session, name: str, description: str, domain: str) -> None:
        """Create or refine one canonical vocabulary entry."""
        await session.exec(
            insert(cls)
            .values(name=cls.canonical(name), description=description, domain=domain)
            .on_conflict_do_update(
                index_elements=["name"], set_={"description": description, "domain": domain}
            )
        )

    @classmethod
    async def extractable_names(cls, session: Session) -> list[str]:
        """Return the sorted vocabulary the extractor may emit."""
        return list(
            await session.exec(
                select(cls.name).where(cls.structural.is_(False)).order_by(cls.name)
            )
        )


class EntityKind(OntologyKind, Timestamped, TableBase, table=True):
    """The live catalog of entity types `EntityContent.type` foreign-keys against."""


class RelationKind(OntologyKind, Timestamped, TableBase, table=True):
    """The live catalog of relation types `FactContent.predicate` foreign-keys against."""
