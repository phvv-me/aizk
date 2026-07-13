from functools import cache

from rls import Catalog
from sqlalchemy import Table, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper, ORMExecuteState, Session, with_loader_criteria

from ..config import settings
from ..exceptions import NoTenantContext
from .mixins import Scoped, TableBase
from .models import FactClaim


def canonicalize_scope_assignment(
    mapper: Mapper[Scoped],
    connection: Connection,
    target: Scoped,
) -> None:
    """Canonicalize scopes before an ORM row reaches PostgreSQL."""
    del mapper, connection
    key = frozenset(target.scopes)
    if not key:
        raise ValueError("scopes cannot be empty")
    target.scopes = sorted(key)


for mapped in TableBase.mapper_registry.mappers:
    model = mapped.class_
    if issubclass(model, Scoped):
        event.listen(model, "before_insert", canonicalize_scope_assignment)
        event.listen(model, "before_update", canonicalize_scope_assignment)


@cache
def protected_tables() -> frozenset[Table]:
    """The RLS-protected tables, read once from the registered metadata."""
    return frozenset(
        table for table in TableBase.metadata.tables.values() if Catalog.state(table) is not None
    )


@event.listens_for(Session, "do_orm_execute")
def require_tenant_context(state: ORMExecuteState) -> None:
    """Refuse scoped ORM statements outside an authorized session."""
    if state.session.info.get("user") is not None:
        return
    scoped = protected_tables()
    if any(table in scoped for mapper in state.all_mappers for table in mapper.tables):
        raise NoTenantContext(
            "scoped query ran outside a user transaction; open the session with `async with user`"
        )


@event.listens_for(Session, "do_orm_execute")
def apply_live_temporal_gate(state: ORMExecuteState) -> None:
    """Apply the shared current-claim predicate to top-level ORM reads."""
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        return
    if state.execution_options.get(settings.skip_live_gate):
        return
    state.statement = state.statement.options(
        with_loader_criteria(FactClaim, lambda cls: cls.is_current, include_aliases=True)
    )
