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
# `Scoped` is the only mixin that reaches for `ScopeLattice` at all (content visibility lives
# beside its consumers in `models.tables.entity`), so nothing else in the package imports
# `ScopeLattice` by name; a reader of this one file sees the whole story, the scopes column, the
# lens, the membership containment, and the policies built from them, together.


class ScopeLattice:
    """The owner/org visibility lattice every `Scoped` table's row level security compiles from.

    Identity is Logto's: there is no local user, org, or membership table. Every scoped row's
    `owner_id` is `uuid5(oidc_subject)` and its `scopes` are `uuid5(oidc_org_id)` values, and the
    caller's own identity and org standing arrive per transaction in GUCs `store.events.bind_user`
    sets straight from the verified token, never a table join. `app.orgs` carries every org the
    caller belongs to (plus the reserved public org), `app.writable_orgs` the subset they may write
    into, so the whole lattice is array containment against a session variable.

    A scoped table builds an instance from its own mapped table (`ScopeLattice(cls.__table__)`), so
    every predicate compiles against the real `owner_id`/`scopes` columns. That self-table
    qualification is harmless since the `rls` catalog comparator strips a policy's own-table column
    qualification before comparing compiled text to the live catalog.

    owner_id: the table's own owner column, read from `table.c` at construction.
    scopes: the table's own scope-set column, read from `table.c` at construction.
    """

    # bound per transaction by `store.events.bind_user`, read once per lattice class: the acting
    # user's uuid, the orgs it belongs to (public org folded in), the orgs it may write, and the
    # optional narrowing lens.
    _uid = rls.current_setting("uid", sa.Uuid(), prefix="app")
    _orgs = rls.current_setting("orgs", ARRAY(sa.Uuid()), prefix="app")
    _writable_orgs = rls.current_setting("writable_orgs", ARRAY(sa.Uuid()), prefix="app")
    _lens = rls.current_setting("scopes", ARRAY(sa.Uuid()), prefix="app")

    def __init__(self, table: Table) -> None:
        self.owner_id = table.c.owner_id
        self.scopes = table.c.scopes

    def read(self) -> ColumnElement[bool]:
        """A row is readable when its scope set clears the reading lens and the reader has standing
        to see it.

        The lens narrows rather than widens. With `app.scopes` unset every row the caller's
        standing reaches is visible; with it set, only a row whose own scope set is fully contained
        by the lens and is not the empty private set passes, the composed-graph projection of one
        subset out of the whole visible union. Standing is ownership, or a shared scope set the
        caller belongs to every org of (`scopes <@ app.orgs`). The public org lives in every
        session's `app.orgs`, so a row scoped to it alone is world-readable with no special branch.
        The `cardinality > 0` guard is load-bearing: `'{}' <@ anything` is trivially true, so
        without it a private (empty-scope) row would be readable by anyone.
        """
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
                    self.scopes.contained_by(self._orgs),
                ),
            ),
        )

    def write(self) -> ColumnElement[bool]:
        """A row is writable when it is the actor's own private row or a scope set they write into.

        Visibility never implies write access. A reader who belongs to an org, or a public-org
        visitor, reads the shared graph but cannot touch it, and ownership alone cannot publish
        into a scope. A multi-org row needs editor-or-admin standing in *every* org it names
        (`scopes <@ app.writable_orgs`), so a bridge claim across two orgs is unwritable by someone
        who writes only one. The same empty-scope `cardinality` guard as `read` keeps a private row
        owner-only.
        """
        return sa.or_(
            sa.and_(sa.func.cardinality(self.scopes) == 0, self.owner_id == self._uid),
            sa.and_(
                sa.func.cardinality(self.scopes) > 0,
                self.scopes.contained_by(self._writable_orgs),
            ),
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
    model with additional policies of its own may override `__rls_policies__` to extend this
    default set (`*super().__rls_policies__()`) rather than editing it here.

    owner_id: user that owns the row, enforced by row level security.
    scopes: the groups this row is shared with, an implicit intersection container rather than one
        administered group. Empty is private to the owner, a singleton is the familiar one-group
        share, and a larger set is the composed graph of every group named, visible only to a
        caller standing in every one of them. `uuid[]` carries no foreign key of its own, Postgres
        has no such constraint on an array element, so a deleted group demotes every row whose set
        contains it back to private explicitly (`Group.delete`) rather than through an `ON DELETE`
        cascade.
    """

    # `uuid5(oidc_subject)`, no foreign key: identity lives in Logto, not a local user table.
    owner_id: uuid.UUID = Field(nullable=False, index=True)
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
