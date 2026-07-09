import uuid
from collections.abc import Iterable

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction, with_loader_criteria

from ..config import settings
from ..exceptions import NoTenantContext
from .identity import PUBLIC_ORG
from .mixins import TableBase
from .models import FactClaim


def pg_uuid_array(ids: Iterable[uuid.UUID]) -> str:
    """Render a uuid set as the Postgres array literal `{a,b,c}` a GUC binds, empty as `''`.

    An empty string rather than `{}` so `rls.current_setting`'s `NULLIF(..., '')` reads an unset
    array as SQL NULL, the "no such standing" sentinel the read and write predicates test for.

    ids: the uuids to render, in the order given.
    """
    rendered = ",".join(str(one) for one in ids)
    return "{" + rendered + "}" if rendered else ""


@event.listens_for(Session, "after_begin")
def bind_user(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
    """Bind the acting identity's four RLS GUCs for the transaction from the session's own `info`.

    A global ORM-level listener rather than a per-engine Core `begin` hook, so every session ever
    opened through `app_sessions` binds the GUCs the moment its transaction starts, with no
    per-engine wiring to remember at construction. Reads the acting user, its org standing, and the
    optional narrowing lens straight off `session.info`, the dict `acting_as` stamps at
    construction, so the identity travels with the session object itself rather than through a
    ContextVar bound around it. `app.orgs` always carries the reserved public org folded in here,
    so every session, member and anonymous alike, reads public-scoped rows with no special branch
    in the policy. `app.writable_orgs` is the subset the token grants editor-or-admin standing in,
    and `app.scopes` the optional read lens. Each binds as a Postgres array literal the same
    `CAST(... AS UUID[])` `rls.current_setting` already applies. Transaction-local (the true
    argument to set_config) so a pooled connection never carries one caller's standing into the
    next transaction, and still bound on every one regardless, since that per-request rebind is the
    tenancy mechanism itself. `docker-compose.yml` declares the anonymous uuid and the empty
    arrays as the GUCs' own defaults, so a session opened outside `acting_as`, carrying no keys,
    already binds to exactly that fallback here.

    session: the session whose transaction just began, its `info` the identity source.
    transaction: the session transaction that just began, unused beyond the event signature.
    connection: the DBAPI connection the transaction runs on, the GUCs bind to.
    """
    uid = session.info.get("user") or settings.anonymous_user_id
    orgs = (*(session.info.get("orgs") or ()), PUBLIC_ORG)
    writable = session.info.get("writable_orgs") or ()
    lens = session.info.get("lens") or ()
    connection.execute(
        text(
            "SELECT set_config('app.uid', :uid, true),"
            " set_config('app.orgs', :orgs, true),"
            " set_config('app.writable_orgs', :writable, true),"
            " set_config('app.scopes', :lens, true)"
        ),
        {
            "uid": str(uid),
            "orgs": pg_uuid_array(orgs),
            "writable": pg_uuid_array(writable),
            "lens": pg_uuid_array(lens),
        },
    )


@event.listens_for(Session, "do_orm_execute")
def require_tenant_context(state: ORMExecuteState) -> None:
    """Refuse an ORM statement against a scoped table when no user is in context.

    The acting user is unset only when a session was opened outside `acting_as`, so a scoped
    read there would silently return nothing. Raising instead surfaces the missing context at the
    call site. Core text statements carry no mapper and so pass through, and the few remaining
    non-scoped tables (ontology, watermarks) an admin session touches are exempt on the same basis.
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
