import uuid

import rls
import sqlalchemy as sa
from sqlalchemy import Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declared_attr
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Field

from ...mixins import Embedded, Id, Scoped, TableBase, Timestamped
from ...mixins.scoped import ScopeLattice


class ContentVisibility:
    """Row level security for a content table, visible only through its own claim table's policies.

    Content carries no owner or scope of its own, so visibility derives through the claim table
    rather than a duplicated predicate here, since the claim table is itself forced under row
    level security, so Postgres applies whichever policies it declares to this subquery's read.
    Deliberate, not incidental. A claim table's visible set is not always just `ScopeLattice.read`
    (`FactClaim` widens it with its own curation-admin escape), so hand-rebuilding only the default
    predicate here would silently drop that wider reach. Lives beside `EntityContent`, its first
    consumer, rather than in `store.rls`, since `FactContent` (`models.tables.fact`) imports
    `content_policies` from here, the one sensible home for a piece two content tables share.

    claim: the claim class this content is visible through, `EntityClaim` or `FactClaim`.
    """

    _id_column = sa.column("id", sa.Uuid())

    def __init__(self, claim: type) -> None:
        self._claim_table = sa.table(claim.__tablename__, sa.column("content_id"))

    def read(self) -> ColumnElement[bool]:
        """A content row is visible when at least one of its claims is visible in its own right."""
        return self._id_column.in_(sa.select(self._claim_table.c.content_id))

    def policies(self) -> list[rls.Policy]:
        """The three policies a content table carries, visible through a claim, freely mintable,
        immutable, and deletable only by a system admin.

        A content row carries no UPDATE policy at all, so any UPDATE is denied outright under
        FORCE ROW LEVEL SECURITY, the database's own enforcement that content, once minted, never
        changes. INSERT is WITH CHECK true since minting content is harmless on its own, real
        access is gated at the claim a caller must separately hold to ever see it again.
        """
        return [
            rls.Policy(name="content_read", command=rls.Command.select, using=self.read()),
            rls.Policy(name="content_insert", command=rls.Command.insert, check=sa.true()),
            rls.Policy(
                name="content_delete", command=rls.Command.delete, using=ScopeLattice.is_admin()
            ),
        ]


def content_policies(claim: type) -> list[rls.Policy]:
    """The three policies a content table carries, visible through `claim`'s own claim table.

    claim: the claim class this content is visible through, its `__tablename__` read directly off
        the class rather than a magic string literal a call site could typo or let drift.
    """
    return ContentVisibility(claim).policies()


class EntityClaim(Id, Scoped, Timestamped, TableBase, table=True):
    """A container's stake in a node, the union that lets a fact belong to A or B.

    Two containers claiming the same content each hold their own row here, so a private note and a
    team's shared graph can both point at the identical deduplicated entity without either seeing
    the other's claim. Row level security on this table is `Scoped`'s ordinary default, since a
    claim is exactly the kind of per-tenant row that default already governs.

    Declared before `EntityContent` in this file, not just after, since `store.rls.register`'s
    mapper-construction hook calls `EntityContent.__rls_policies__` synchronously the moment
    `EntityContent`'s own class statement finishes, before the rest of the module runs, so it can
    only resolve a bare `EntityClaim` name already bound in module globals by then, not one defined
    later in the same file.

    id: uuid7 claim identity.
    content_id: the entity content this claim stakes, cascading on delete so the last claim's
        removal is what makes a content row eligible for the system merge's own cleanup.
    owner_id: principal that holds this claim, enforced by row level security.
    scopes: group set this claim is shared with, an implicit intersection when it names more than
        one, empty when private to the owner.
    attributes: free-form structured detail extracted alongside this container's claim.
    created_at: when this container first staked the claim.
    """

    content_id: uuid.UUID = Field(
        foreign_key="entity_content.id", ondelete="CASCADE", nullable=False, index=True
    )
    attributes: dict = Field(
        default_factory=dict, sa_column_kwargs={"server_default": "{}"}, sa_type=JSONB
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[UniqueConstraint | Index, ...]:
        # one live claim per container per content: a `uuid[]` carries no NULL to fold, an empty
        # array is its own ordinary, comparable value, so plain array equality already folds every
        # private claim into one uniqueness class with no NULLS NOT DISTINCT needed. No
        # `*super().__table_args__` here, unlike `EntityContent`/`FactContent`/`FactClaim`: those
        # all mix in `Embedded`, whose own `__table_args__` this composes with, but a claim carries
        # no embedding of its own so nothing earlier in its MRO declares the attribute to extend.
        # ix_entity_claim_scopes is a GIN index over the scope-set array, entity_claim is a hot
        # table for the containment reads `mixins.scoped.ScopeLattice`'s policies run on every
        # visible row.
        return (
            UniqueConstraint(
                "content_id",
                "owner_id",
                "scopes",
                name="uq_entity_claim_content_owner_scope",
            ),
            Index("ix_entity_claim_scopes", "scopes", postgresql_using="gin"),
        )


class EntityContent(Id, Embedded, TableBase, table=True):
    """The immutable, deduplicated identity of a graph node, content-addressed and tenant-free.

    A node's name, type, and embedding are structural knowledge, not any one container's private
    fact, so they are minted once and shared. Two owners extracting the same name and type land
    one content row, each holding their own `EntityClaim` on it. Visible only through a claim,
    never directly, `__rls_policies__` below declares its custom read-through-claim,
    freely-mintable, immutable shape rather than inheriting `Scoped`'s owner/scope policies, since
    this table carries neither column.

    id: content-addressed identity from uuid5 over normalized name and type.
    name: canonical surface form of the entity.
    type: entity type, foreign-keyed against the live `entity_kind` catalog, the wall that keeps
        a stray or off-vocabulary type from ever reaching a row regardless of what path wrote it.
    embedding: halfvec dense vector of the name, null until embedded, stored once regardless of how
        many containers hold a claim on this content.
    """

    name: str = Field(sa_type=Text)
    type: str = Field(sa_type=Text, foreign_key="entity_kind.name")

    @classmethod
    def __rls_policies__(cls) -> list[rls.Policy]:
        """Visible through an `entity_claim`, freely mintable, immutable, admin-only to delete."""
        return content_policies(EntityClaim)
