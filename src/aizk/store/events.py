from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction, with_loader_criteria

from ..config import settings
from ..exceptions import NoTenantContext
from .mixins import TableBase
from .models import FactClaim


@event.listens_for(Session, "after_begin")
def bind_user(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
    """Bind app.uid and app.scopes for the transaction from the session's own acting identity.

    A global ORM-level listener rather than a per-engine Core `begin` hook, so every session ever
    opened through `app_sessions` binds the GUCs the moment its transaction starts, with no
    per-engine wiring to remember at construction. Reads the acting user and the optional
    narrowing lens straight off `session.info`, the dict `acting_as` stamps at construction, so the
    identity travels with the session object itself rather than through a ContextVar bound around
    it. The lens binds as a Postgres array literal (`{a,b,c}`), the same `CAST(... AS UUID[])`
    `rls.current_setting` already applies to a scalar GUC parsing it with no extra step on the read
    side. Transaction-local (the true argument to set_config) so a pooled connection never carries
    a scope into the next transaction, and still bound on every transaction regardless, since that
    per-request rebind is the tenancy mechanism itself. `postgres.conf` (`docker-compose.yml`)
    declares the anonymous uuid and an empty lens as the GUCs' own defaults, so a session opened
    outside `acting_as`, carrying neither key, already binds to exactly that fallback here.

    session: the session whose transaction just began, its `info` the identity source.
    transaction: the session transaction that just began, unused beyond the event signature.
    connection: the DBAPI connection the transaction runs on, the GUCs bind to.
    """
    uid = session.info.get("user") or settings.anonymous_user_id
    narrowed = session.info.get("lens") or ()
    lens = "{" + ",".join(str(group_id) for group_id in narrowed) + "}" if narrowed else ""
    connection.execute(
        text("SELECT set_config('app.uid', :uid, true), set_config('app.scopes', :lens, true)"),
        {"uid": str(uid), "lens": lens},
    )


@event.listens_for(Session, "do_orm_execute")
def require_tenant_context(state: ORMExecuteState) -> None:
    """Refuse an ORM statement against a scoped table when no user is in context.

    The acting user is unset only when a session was opened outside `acting_as`, so a scoped
    read there would silently return nothing. Raising instead surfaces the missing context at the
    call site. Core text statements carry no mapper and so pass through, leaving writes to the
    non-scoped identity tables (users, groups, memberships) untouched.
    """
    if state.session.info.get("user") is not None:
        return
    scoped = {
        TableBase.metadata.tables[name] for name in TableBase.metadata.info.get("rls", set())
    }
    if any(table in scoped for mapper in state.all_mappers for table in mapper.tables):
        raise NoTenantContext(
            "scoped query ran without `acting_as`; open the session under a user"
        )


@event.listens_for(Session, "do_orm_execute")
def apply_live_temporal_gate(state: ORMExecuteState) -> None:
    """Gate every live-graph claim read to the current version, one loader criteria.

    `with_loader_criteria` registers the temporal `FactClaim.is_current` gate on each top-level
    select, so every fact lane, and any future relationship load of claims, shares one criteria
    application rather than each re-deriving the predicate by hand. This is temporal correctness in
    the ORM, never the security boundary, which stays in Postgres row level security. A read that
    must see history opts out with `settings.skip_live_gate` and lists its own predicates, namely
    the as_of replay, the raw count, existence, and promote-copy reads, and the export dump. Column
    and relationship loads inherit the parent statement's criteria, so they are skipped here to
    avoid applying it a redundant second time.
    """
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        return
    if state.execution_options.get(settings.skip_live_gate):
        return
    state.statement = state.statement.options(
        with_loader_criteria(FactClaim, lambda cls: cls.is_current, include_aliases=True)
    )
