from typing import Any, ClassVar

import rls
import sqlalchemy as sa
from patos import sql
from pydantic import UUID5
from sqlalchemy import Table, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CompoundSelect
from sqlmodel import select

from ...config import settings
from ..identity import User


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

    @staticmethod
    def _authority(standing: ColumnElement[Any], permission: str) -> ColumnElement[list[UUID5]]:
        """Turn one JSON scope permission into a native PostgreSQL UUID array."""
        values = (
            sa.func.jsonb_array_elements_text(standing.op("->")(permission))
            .table_valued("value")
            .render_derived()
        )
        return sa.func.array(select(values.c.value.cast(Uuid())).scalar_subquery())

    @classmethod
    def __rls__(cls) -> tuple[rls.Policy, ...]:
        """Require complete standing in the row's scope intersection."""
        scopes = cls.scopes
        standing = User.setting("scopes")
        writable = cls._authority(standing, "write")
        nonempty = sa.func.cardinality(scopes) > 0
        if parent_name := cls.read_through:
            parent = sa.table(
                parent_name,
                sa.column("id", Uuid()),
                sa.column("scopes", ARRAY(Uuid())),
            )
            parent_id = cls.__table__.c[f"{parent_name}_id"]
            read: ColumnElement[bool] = cls.__table__.c[f"{parent_name}_id"].in_(
                select(parent.c.id)
            )
            parent_scope: ColumnElement[bool] = sa.tuple_(parent_id, scopes).in_(
                select(parent.c.id, parent.c.scopes)
            )
        else:
            readable = cls._authority(standing, "read")
            public = cls._authority(standing, "public")
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
