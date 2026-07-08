import uuid
from typing import cast

import rls
import sqlalchemy as sa
from sqlalchemy import Table, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Field

from .base import TableBase

# `Scoped` and the visibility lattice its default policies compile from live in the same file:
# `Scoped` is the only mixin that reaches for `ScopeLattice` at all (the curated-canon escape
# `FactClaim` layers on top lives beside `FactClaim` itself in `models.tables.fact`, and content
# visibility lives beside its consumers in `models.tables.entity`), so nothing else in the package
# imports `ScopeLattice` by name; a reader of this one file sees the whole story, the scopes
# column, the lens, the membership containment, and the policies built from them, together.


class ScopeLattice:
    """The owner/scope visibility lattice every `Scoped` table's row level security compiles from.

    A scoped table builds an instance from its own mapped table (`ScopeLattice(cls.__table__)`),
    so every predicate compiles against the real `owner_id`/`scopes` columns rather than a bare
    cross-file stand-in a caller had to import back. That self-table qualification is harmless
    since the `rls` library's own catalog comparator (`rls.normalize._unqualify`) already strips a
    policy's own-table column qualification before ever comparing compiled text to the live
    catalog, so a drift check never sees it.

    owner_id: the table's own owner column, read from `table.c` at construction.
    scopes: the table's own scope-set column, read from `table.c` at construction.
    """

    # the acting user and the optional scope-set reading lens, GUCs
    # `store.events.bind_user` binds per transaction; read once per lattice class rather than
    # per predicate call.
    _uid = rls.current_setting("uid", sa.Uuid(), prefix="app")
    _lens = rls.current_setting("scopes", ARRAY(sa.Uuid()), prefix="app")

    # Core table stand-ins for the visibility lattice, columns named but untyped, so a predicate
    # can join against membership, groups, and user without importing their mapped ORM
    # classes: a mixin's predicates are built before its own concrete subclasses exist, and
    # importing the model modules here would cycle back through `mixins`, which every model itself
    # imports.
    _membership = sa.table(
        "membership", sa.column("user_id"), sa.column("group_id"), sa.column("role")
    )
    _groups = sa.table("group_", sa.column("id"), sa.column("public"), sa.column("curated"))
    _users = sa.table("users", sa.column("id"), sa.column("is_admin"))

    def __init__(self, table: Table) -> None:
        self.owner_id = table.c.owner_id
        self.scopes = table.c.scopes

    @classmethod
    def empty_scopes(cls) -> ColumnElement:
        """The empty-array literal every "private" comparison and coalesce fallback shares.

        A fresh cast each call, not a shared instance, since SQLAlchemy expressions are cheap,
        immutable value objects, and every call site compiles to the identical text either way,
        the only thing a drift check or a query plan ever compares.
        """
        return sa.cast(sa.literal("{}"), ARRAY(sa.Uuid()))

    @classmethod
    def is_admin(cls) -> ColumnElement[bool]:
        """Whether the acting user carries the server-wide admin flag."""
        return sa.exists(
            sa.select(sa.literal(1)).where(cls._users.c.id == cls._uid, cls._users.c.is_admin)
        )

    @classmethod
    def _group_array(
        cls, condition: ColumnElement[bool], role_filter: ColumnElement[bool] | None = None
    ) -> ColumnElement:
        """A `coalesce(array_agg(group_id), '{}')` scalar subquery of the acting user's own
        groups.

        Every containment check below (`scopes <@ ...`) needs an array on both sides, but
        `membership` is a row-per-group table, so its matching group ids are aggregated into one
        array here rather than at each call site. `coalesce` turns "no membership rows at all"
        into the empty array, never a SQL `NULL` a `<@` comparison would otherwise silently fail
        against.

        condition: the membership predicate selecting this user's own rows, `user_id =
            uid` further narrowed by the caller (e.g. only admin-role rows).
        role_filter: an additional predicate on `role`, folded into `condition` when given.
        """
        where = sa.and_(condition, role_filter) if role_filter is not None else condition
        return (
            sa.select(
                sa.func.coalesce(sa.func.array_agg(cls._membership.c.group_id), cls.empty_scopes())
            )
            .where(where)
            .scalar_subquery()
        )

    @classmethod
    def curated_group_ids(cls) -> ColumnElement:
        """Coalesced array of every group id currently marked curated.

        A generally useful lattice fact on its own, not only the curation-admin escape's own
        gadget: any policy or query that needs "which groups govern their canon by review" reads
        it from here rather than re-deriving the same `curated` filter.
        """
        return (
            sa.select(sa.func.coalesce(sa.func.array_agg(cls._groups.c.id), cls.empty_scopes()))
            .where(cls._groups.c.curated)
            .scalar_subquery()
        )

    @classmethod
    def admin_group_ids(cls) -> ColumnElement:
        """Coalesced array of group ids the acting user holds the admin membership role in."""
        return cls._group_array(
            cls._membership.c.user_id == cls._uid, cls._membership.c.role == "admin"
        )

    def read(self) -> ColumnElement[bool]:
        """A row is readable when its scope set clears the reading lens and the reader has
        standing.

        The lens narrows rather than widens. With `app.scopes` unset every row standing already
        reaches is visible, and with it set only a row whose own scope set is fully contained by
        the lens and is not itself the empty private set passes, the composed-graph projection of
        one subset out of the caller's whole visible union. A private row (`cardinality(scopes) =
        0`) is excluded once a lens is set. It stays reachable only with no lens at all, since a
        lens is "a different space" for that combination of groups, not a wider window onto the
        owner's own private layer. Standing itself is ownership, a scope set the reader stands in
        every member of (shared membership), or a scope set that is exactly one public group's
        singleton (the narrower, single-group shape a public share is kept to, never an implicit
        multi-group intersection).
        """
        member_groups = self._group_array(self._membership.c.user_id == self._uid)
        public_groups = sa.select(self._groups.c.id).where(self._groups.c.public)
        return sa.and_(
            sa.or_(
                self._lens.is_(None),
                sa.and_(
                    self.scopes.contained_by(self._lens), sa.func.cardinality(self.scopes) > 0
                ),
            ),
            sa.or_(
                self.owner_id == self._uid,
                sa.and_(
                    sa.func.cardinality(self.scopes) > 0,
                    self.scopes.contained_by(member_groups),
                ),
                sa.and_(sa.func.cardinality(self.scopes) == 1, self.scopes[1].in_(public_groups)),
            ),
        )

    def write(self) -> ColumnElement[bool]:
        """A row is writable when it is the actor's own private row or a scope set they write into.

        Visibility never implies write access. A reader member and a public-group visitor read the
        shared graph but cannot touch it, and ownership alone cannot publish into a scope, the moat
        the promote path relies on. Writing a multi-group row needs writer-or-admin standing in
        *every* group the set names, the same containment shape `read`'s member branch uses, so a
        bridge claim spanning two groups can never be written by someone who only writes one of
        them.
        """
        writer_groups = self._group_array(
            self._membership.c.user_id == self._uid,
            self._membership.c.role.in_(("writer", "admin")),
        )
        return sa.or_(
            sa.and_(sa.func.cardinality(self.scopes) == 0, self.owner_id == self._uid),
            self.scopes.contained_by(writer_groups),
        )

    def default_policies(self) -> list[rls.Policy]:
        """The four per-command scope policies every `Scoped` table carries.

        The write predicate is split across per-command policies rather than one `FOR ALL` policy,
        since a `FOR ALL` policy's USING clause would also be OR-ed into SELECT visibility and leak
        writable rows past the read policy's narrowing lens.
        """
        read = self.read()
        write = self.write()
        return [
            rls.Policy(name="scope_read", command=rls.Command.select, using=read),
            rls.Policy(name="scope_insert", command=rls.Command.insert, check=write),
            rls.Policy(name="scope_update", command=rls.Command.update, using=write, check=write),
            rls.Policy(name="scope_delete", command=rls.Command.delete, using=write),
        ]


class Scoped:
    """Row level security columns mixed into every tenant-scoped table.

    Each concrete subclass registers its auto-derived table name under
    `TableBase.metadata.info['rls']` so the Alembic autogenerate comparator can prove every scoped
    table forces the per-command scope policies and a new scoped model can never ship without them,
    and declares `__rls_policies__`, the default read/write scope policies every scoped table
    carries, read by `store.rls.register`'s mapper-construction hook once the table exists. A
    model with additional policies of its own, `FactClaim`'s curation-admin escape, overrides
    `__rls_policies__` to extend this default set (`*super().__rls_policies__()`) rather than
    editing it here.

    owner_id: user that owns the row, enforced by row level security.
    scopes: the groups this row is shared with, an implicit intersection container rather than one
        administered group. Empty is private to the owner, a singleton is the familiar one-group
        share, and a larger set is the composed graph of every group named, visible only to a
        caller standing in every one of them. `uuid[]` carries no foreign key of its own, Postgres
        has no such constraint on an array element, so a deleted group demotes every row whose set
        contains it back to private explicitly (`Group.delete`) rather than through an `ON DELETE`
        cascade.
    """

    owner_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    # `sa_type=`/`sa_column_kwargs=` rather than a literal `sa_column=Column(...)`: a mixin's own
    # class body runs once, so a fully constructed `Column` object assigned there would be the
    # exact same instance every subclass inherits, and SQLAlchemy refuses to attach one physical
    # Column to more than one Table ("Column object 'scopes' already assigned to Table ..."). The
    # `sa_type=`/`sa_column_kwargs=` shorthand instead lets each concrete model's own SQLModel
    # metaclass build its own fresh Column from these arguments, the same reason `owner_id` above
    # never passes a raw `Column` either. The `cast` mirrors `Embedded.embedding`'s own:
    # `Field.sa_type` types as `type[T]`, a bare class, but SQLModel accepts (and every other
    # parametrized column in this codebase already passes) an instantiated `TypeEngine` like
    # `ARRAY(Uuid())` at runtime, only the stub gap needing the cast.
    scopes: list[uuid.UUID] = Field(
        default_factory=list,
        sa_type=cast(type[list[uuid.UUID]], ARRAY(Uuid())),
        sa_column_kwargs={"server_default": text("'{}'")},
    )

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        table = getattr(cls, "__tablename__", None)
        if isinstance(table, str):
            TableBase.metadata.info.setdefault("rls", set()).add(table)

    @classmethod
    def __rls_policies__(cls) -> list[rls.Policy]:
        """The default scope_read/scope_insert/scope_update/scope_delete policies, this table's."""
        return ScopeLattice(cls.__table__).default_policies()
