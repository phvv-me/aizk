from sqlalchemy import Boolean, Text, select
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Field

from ...engine import session
from ...mixins import TableBase, Timestamped


class OntologyKind:
    """The shared shape `EntityKind` and `RelationKind` both carry, the live catalog
    `EntityContent.type`/`FactContent.predicate` foreign-key against instead of a hardcoded
    `CHECK` constraint.

    Growing the vocabulary is an ordinary row insert, never a schema migration, `mint` below or
    the extraction pipeline's own auto-create cascade. The catalog only ever grows, never
    deletes, matching how the schema-induction papers this design follows treat a vocabulary,
    grow by induction and canonicalize near-duplicates at create time (the auto-create cascade's
    own similarity fold), with no manual retire step. `name` is the primary key since the name
    itself, "Decision", "proves", is the identity a content row's `type`/`predicate` column
    actually stores, a surrogate id here would answer to nothing.

    name: the vocabulary member itself, what a content row's `type` or `predicate` column stores.
    description: one-line gloss rendered into the extraction prompt and matched against a
        suggested type the auto-create cascade is deciding whether to fold in or mint fresh.
    domain: a plain grouping tag ("core", "general", "coding", "research", "finance",
        "personal", or "auto" for an auto-created row), never its own closed vocabulary, so
        tagging a new area never needs a schema change either.
    structural: whether this member is system-written only, the RAPTOR tree and the reflective
        insight pass's own types and predicate, never one the extractor may emit, so
        `extractable_names` filters it out of the prompt vocabulary.
    """

    name: str = Field(sa_type=Text, primary_key=True)
    description: str = Field(sa_type=Text)
    domain: str = Field(sa_type=Text)
    structural: bool = Field(
        default=False, sa_type=Boolean, sa_column_kwargs={"server_default": "false"}
    )

    @classmethod
    async def mint(cls, name: str, description: str, domain: str) -> None:
        """Insert one catalog row, tolerating a name another writer already minted.

        The auto-create cascade's own write, `ON CONFLICT DO NOTHING` on the name itself since two
        concurrent extractions independently deciding on the identical new type name should
        converge on one row rather than racing a unique-violation. No savepoint needed since this
        catalog carries no row level security for a conflicting insert to hide behind.

        name: the new vocabulary member, the extractor's own `suggested_type` once accepted.
        description: one-line gloss, the suggestion's own grounding statement.
        domain: grouping tag, "auto" for an auto-created row.
        """
        await session().execute(
            insert(cls)
            .values(name=name, description=description, domain=domain)
            .on_conflict_do_nothing(index_elements=["name"])
        )

    @classmethod
    async def define(cls, name: str, description: str, domain: str) -> None:
        """Create or update one catalog row, the deliberate curator write the agent drives.

        Where `mint` leaves an existing row untouched, since it exists only to converge two racing
        extractions onto one name, this is the admin or harness-agent write and updates the gloss
        and domain of a name already present, so the agent can sharpen a type's description or
        retag its domain without a migration. Never deletes, the catalog stays grow-only, so
        curation here is always add-or-refine and the bookkeeping the agent is good at costs
        nothing but a row write and a snapshot refresh.

        name: the vocabulary member, the type or predicate a content row stores.
        description: one-line gloss rendered into the extraction prompt and matched by the
            auto-create similarity fold.
        domain: grouping tag such as core, general, coding, research, finance, or personal.
        """
        await session().execute(
            insert(cls)
            .values(name=name, description=description, domain=domain)
            .on_conflict_do_update(
                index_elements=["name"], set_={"description": description, "domain": domain}
            )
        )

    @classmethod
    async def extractable_names(cls) -> list[str]:
        """Every non-structural member's name, sorted for a byte-stable, reproducible prompt, the
        extraction vocabulary a caller may actually emit.
        """
        return sorted(await session().scalars(select(cls.name).where(~cls.structural)))


class EntityKind(OntologyKind, Timestamped, TableBase, table=True):
    """The live catalog of entity types `EntityContent.type` foreign-keys against."""


class RelationKind(OntologyKind, Timestamped, TableBase, table=True):
    """The live catalog of relation types `FactContent.predicate` foreign-keys against."""
