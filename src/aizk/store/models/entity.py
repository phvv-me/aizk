import uuid

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declared_attr, validates
from sqlmodel import Field

from ...exceptions import OntologyError
from ...extract.ontology import EntityType, check_in_sql
from ..mixins import Embedded, Id, Scoped, TableBase, Timestamped
from ..rls import Policy, content_policies


class EntityContent(Id, Embedded, TableBase, table=True):
    """The immutable, deduplicated identity of a graph node, content-addressed and tenant-free.

    A node's name, type, and embedding are structural knowledge, not any one container's private
    fact, so they are minted once and shared: two owners extracting the same name and type land one
    content row, each holding their own `EntityClaim` on it. Visible only through a claim, never
    directly, `__rls_policies__` below declares its custom read-through-claim, freely-mintable,
    immutable shape rather than inheriting `Scoped`'s owner/scope policies, since this table
    carries neither column.

    id: content-addressed identity from uuid5 over normalized name and type.
    name: canonical surface form of the entity.
    type: ontology entity type drawn from the closed vocabulary.
    embedding: halfvec dense vector of the name, null until embedded, stored once regardless of how
        many containers hold a claim on this content.
    """

    name: str = Field(sa_type=Text)
    type: str = Field(sa_type=Text)

    @classmethod
    def __rls_policies__(cls) -> list[Policy]:
        """Visible through an `entity_claim`, freely mintable, immutable, admin-only to delete."""
        return content_policies("entity_claim")

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | CheckConstraint, ...]:
        # a database-level third wall mirroring `validate_type`, the same `EntityType` membership
        # the 0001 migration's `ck_entity_content_type` constraint checks, built from the same
        # `check_in_sql` call so autogenerate never sees the two sides drift.
        return (
            *super().__table_args__,
            CheckConstraint(check_in_sql("type", EntityType), name="ck_entity_content_type"),
        )

    @validates("type")
    def validate_type(self, key: str, value: str) -> str:
        """Reject an entity type outside the closed ontology so the ORM boundary fails off-vocab.

        The extractor already renders the ontology as enums, and this is the second wall, so a type
        reaching the row by any path other than extraction, a hand-built row or a future caller, is
        held to the same closed vocabulary, `EntityType`'s structural members (the RAPTOR summary
        and insight observation types the system writes itself) included.

        key: the attribute being set, always `type`.
        value: the candidate entity type to admit or reject.
        """
        if value not in set(EntityType):
            raise OntologyError(f"entity type {value!r} is not in the ontology")
        return value


class EntityClaim(Id, Scoped, Timestamped, TableBase, table=True):
    """A container's stake in a node, the union that lets a fact belong to A or B.

    Two containers claiming the same content each hold their own row here, so a private note and a
    team's shared graph can both point at the identical deduplicated entity without either seeing
    the other's claim; row level security on this table is `Scoped`'s ordinary default, since a
    claim is exactly the kind of per-tenant row that default already governs.

    id: uuid7 claim identity.
    content_id: the entity content this claim stakes, cascading on delete so the last claim's
        removal is what makes a content row eligible for the system merge's own cleanup.
    owner_id: principal that holds this claim, enforced by row level security.
    scope: group this claim is shared with, null when private to the owner.
    attributes: free-form structured detail extracted alongside this container's claim.
    created_at: when this container first staked the claim.
    """

    content_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("entity_content.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    attributes: dict = Field(
        default_factory=dict, sa_column_kwargs={"server_default": "{}"}, sa_type=JSONB
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[UniqueConstraint, ...]:
        # one live claim per container per content: NULLS NOT DISTINCT folds every private claim
        # (scope NULL) into the same uniqueness class as a scoped one, so a container cannot double
        # claim a node under its own private umbrella either. No `*super().__table_args__` here,
        # unlike `EntityContent`/`FactContent`/`FactClaim`: those all mix in `Embedded`, whose own
        # `__table_args__` this composes with, but a claim carries no embedding of its own so
        # nothing earlier in its MRO declares the attribute to extend.
        return (
            UniqueConstraint(
                "content_id",
                "owner_id",
                "scope",
                name="uq_entity_claim_content_owner_scope",
                postgresql_nulls_not_distinct=True,
            ),
        )
