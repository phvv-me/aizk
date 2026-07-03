import uuid

from sqlalchemy import Uuid, column
from sqlmodel import Field

from ..rls import Policy, default_scope_policies
from .base import TableBase

# bare, table-unqualified stand-ins for every scoped table's own owner_id/scope columns. A
# `CREATE POLICY` clause is always scoped to exactly one table, so it never needs table
# qualification, and Postgres's own catalog re-serializes a stored policy's `qual`/`with_check`
# with qualification already stripped; building every policy against these from the start, rather
# than a real mapped `Column` off `cls.__table__.c`, keeps the freshly compiled text and the live
# catalog text in the same unqualified shape with no extra normalization to bridge the two.
OWNER_ID = column("owner_id", Uuid())
SCOPE = column("scope", Uuid())


class Scoped:
    """Row level security columns mixed into every tenant-scoped table.

    Each concrete subclass registers its auto-derived table name under
    `TableBase.metadata.info['rls']` so the Alembic autogenerate comparator can prove every scoped
    table forces the per-command scope policies and a new scoped model can never ship without them,
    and declares `__rls_policies__`, the default read/write scope policies every scoped table
    carries, read by `store.rls.register`'s mapper-construction hook once the table exists. A
    model with additional policies of its own, `FactClaim`'s curation-admin escape, overrides
    `__rls_policies__` to extend this default set rather than editing it here.

    owner_id: principal that owns the row, enforced by row level security.
    scope: group the row is shared with, null when private to the owner. Deleting a group nulls
        the reference, so its rows fall back to private rather than blocking the delete.
    """

    owner_id: uuid.UUID = Field(foreign_key="principal.id", nullable=False, index=True)
    scope: uuid.UUID | None = Field(default=None, foreign_key="group_.id", ondelete="SET NULL")

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        table = getattr(cls, "__tablename__", None)
        if isinstance(table, str):
            TableBase.metadata.info.setdefault("rls", set()).add(table)

    @classmethod
    def __rls_policies__(cls) -> list[Policy]:
        """The default scope_read/scope_insert/scope_update/scope_delete policies, this table's."""
        return default_scope_policies(OWNER_ID, SCOPE)
