import uuid
from typing import ClassVar, cast

import rls
import sqlalchemy as sa
from sqlalchemy import Table, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Field

from ...common.sql import Column
from ...config import settings
from ..identity import User


class Scoped:
    """A nonempty scope intersection with inherited PostgreSQL row security."""

    __table__: ClassVar[Table]
    mutable: ClassVar[bool] = False
    deletable: ClassVar[bool] = False
    read_through: ClassVar[str | None] = None

    created_by: Column[uuid.UUID] = Field(nullable=False, index=True)
    scopes: Column[list[uuid.UUID]] = Field(
        min_length=1,
        sa_type=cast(type[list[uuid.UUID]], ARRAY(Uuid())),
    )

    @staticmethod
    def _authority(standing: ColumnElement, permission: str) -> ColumnElement[list[uuid.UUID]]:
        """Turn one JSON scope permission into a native PostgreSQL UUID array."""
        values = (
            sa.func.jsonb_array_elements_text(standing.op("->")(permission))
            .table_valued("value")
            .render_derived()
        )
        return sa.func.array(sa.select(sa.cast(values.c.value, Uuid())).scalar_subquery())

    @classmethod
    def __rls__(cls) -> tuple[rls.Policy, ...]:
        """Require complete standing in the row's scope intersection."""
        scopes = cls.scopes
        standing = User.setting("scopes")
        writable = cls._authority(standing, "write")
        nonempty = sa.func.cardinality(scopes) > 0
        if parent_name := cls.read_through:
            parent = sa.table(parent_name, sa.column("id", Uuid()))
            read = cls.__table__.c[f"{parent_name}_id"].in_(sa.select(parent.c.id))
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
        write = sa.and_(nonempty, scopes.op("<@")(writable))
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
