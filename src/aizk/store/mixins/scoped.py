from typing import ClassVar, cast

import rls
import sqlalchemy as sa
from patos import sql
from pydantic import UUID5
from sqlalchemy import Table, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CompoundSelect, ScalarSelect
from sqlmodel import select

from ...config import DatabaseBackend, settings
from ..identity import User

# Where each authority list sits in the CockroachDB `application_name` encoding.
_ENCODED_POSITIONS = {"read": 2, "write": 3, "public": 4}


class Standing:
    """The caller's read, write, and public scope authority, read back inside the database.

    Row security spends this authority to decide what a caller may see. The counting
    surfaces spend it again to decide what is the caller's own memory, which is a stricter
    question than visibility and belongs beside the policy that answers the first one.
    """

    @staticmethod
    def authority(permission: str) -> ColumnElement[list[UUID5]]:
        """Resolve one permission's scope ids from the transaction-local caller context."""
        if settings.database_backend is DatabaseBackend.cockroachdb:
            encoded = sa.func.nullif(
                sa.func.split_part(
                    sa.func.current_setting("application_name", True),
                    "|",
                    _ENCODED_POSITIONS[permission],
                ),
                "",
            )
            return cast(ColumnElement[list[UUID5]], encoded.cast(ARRAY(Uuid())))
        values = (
            sa.func.jsonb_array_elements_text(User.setting("scopes").op("->")(permission))
            .table_valued("value")
            .render_derived()
        )
        return sa.func.array(select(values.c.value.cast(Uuid())).scalar_subquery())

    @classmethod
    def counted(cls, scopes: ColumnElement[list[UUID5]]) -> ColumnElement[bool]:
        """Whether a row is the caller's own memory rather than one it merely reads.

        A public organization is readable by everyone, so counting it would open a new
        account on tens of thousands of items it never wrote. A row drops out of the counts
        only when it lives entirely in public scopes the caller cannot write. The private
        scope and every member organization always count, and so does a row a caller filed
        into one of its own scopes alongside a public one, since that row is its own work.
        Reading is untouched, so this narrows the numbers presented as the caller's memory
        and never what recall may retrieve.
        """
        borrowed = sa.func.unnest(cls.authority("public")).table_valued("scope").render_derived()
        unwritable = sa.func.array(
            select(borrowed.c.scope)
            .where(borrowed.c.scope != sa.all_(cls.authority("write")))
            .scalar_subquery()
        )
        return ~scopes.op("<@")(unwritable)

    @classmethod
    def owned_total(cls, table: Table, *restrictions: ColumnElement[bool]) -> ScalarSelect[int]:
        """Count one relation's visible rows that count as the caller's own memory."""
        return (
            select(sa.func.count(table.c.id))
            .where(cls.counted(table.c.scopes), *restrictions)
            .scalar_subquery()
        )


class Scoped(sql.Model):
    """Authorize one nonempty scope intersection entirely inside PostgreSQL.

    A caller may read a row only when every stored scope is readable. A caller
    may write a row only when every stored scope is writable. Child rows that
    set `read_through` inherit visibility from their parent and must store the
    same scopes as that visible parent, which prevents cross-tenant child rows.
    """

    __table__: ClassVar[Table]
    mutable: ClassVar[bool] = False
    deletable: ClassVar[bool] = False
    read_through: ClassVar[str | None] = None

    created_by = sql.Field(UUID5, index=True)
    # A scope set is stored as an ordered array, so equality and any unique index over it
    # only mean set identity while every writer sorts. Every writer does: the scope set
    # always arrives from `User.write_scope` or a `sorted(...)` at the call site, and
    # migration `0006_document_promotion_identity` sorts the rows written before that
    # invariant was relied upon. A row inserted around the ORM would break it, which is why
    # raw inserts into scoped tables belong in tests and migrations alone.
    scopes = sql.Field(
        list[UUID5],
        min_length=1,
        sa_type=ARRAY(Uuid()),
        server_default=sa.text("'{}'"),
    )

    @classmethod
    def scope_sets(cls, *peers: type[Scoped]) -> CompoundSelect[tuple[list[UUID5]]]:
        """Every distinct stored scope array across this table and its peers."""
        return select(cls.scopes).union(*(select(peer.scopes) for peer in peers))

    @classmethod
    def __rls__(cls) -> tuple[rls.Policy, ...]:
        """Require complete standing in the row's scope intersection."""
        scopes = cls.scopes
        readable = Standing.authority("read")
        writable = Standing.authority("write")
        public = Standing.authority("public")
        nonempty = sa.func.cardinality(scopes) > 0
        if parent_name := cls.read_through:
            parent_id = cls.__table__.c[f"{parent_name}_id"]
            if settings.database_backend is DatabaseBackend.cockroachdb:
                visible = getattr(sa.func, f"aizk_{parent_name}_visible")
                read = visible(parent_id, scopes)
                parent_scope = read
            else:
                parent = sa.table(
                    parent_name,
                    sa.column("id", Uuid()),
                    sa.column("scopes", ARRAY(Uuid())),
                )
                read = parent_id.in_(select(parent.c.id))
                parent_scope = sa.tuple_(parent_id, scopes).in_(
                    select(parent.c.id, parent.c.scopes)
                )
        else:
            read = sa.and_(
                nonempty,
                sa.or_(
                    scopes.op("<@")(readable),
                    sa.and_(
                        sa.func.cardinality(scopes) == 1,
                        scopes.op("<@")(public),
                    ),
                ),
            )
            parent_scope = sa.true()
        write = sa.and_(nonempty, scopes.op("<@")(writable), parent_scope)
        policies = [
            rls.Policy.select("scope_read", read, roles=(settings.app_role,)),
            rls.Policy.insert("scope_insert", write, roles=(settings.app_role,)),
        ]
        if cls.mutable:
            policies.append(
                rls.Policy.update("scope_update", write, write, roles=(settings.app_role,))
            )
        if cls.deletable:
            policies.append(rls.Policy.delete("scope_delete", write, roles=(settings.app_role,)))
        return tuple(policies)
