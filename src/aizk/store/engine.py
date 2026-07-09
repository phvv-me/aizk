import functools
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import settings
from ..exceptions import NoTenantContext

_current_session: ContextVar[AsyncSession] = ContextVar("aizk_session")

# the caller's org standing for the enclosing request or operator operation, the orgs it belongs
# to and the subset it may write, read by every `acting_as` the block opens. A request-constant,
# so it rides task-local context set once at a boundary (the MCP identity middleware, an operator
# op that grants itself standing) rather than threading through every internal signature down to a
# retrieval lane's own `acting_as`. The default is empty standing, the fail-safe: a call outside
# any boundary sees only what it owns plus the public scope, never more, so a forgotten
# `caller_standing` narrows visibility rather than widening it.
_caller_standing: ContextVar[tuple[tuple[uuid.UUID, ...], tuple[uuid.UUID, ...]]] = ContextVar(
    "aizk_standing", default=((), ())
)


def current_standing() -> tuple[tuple[uuid.UUID, ...], tuple[uuid.UUID, ...]]:
    """The `(orgs, writable_orgs)` the enclosing `caller_standing` block set, empty by default."""
    return _caller_standing.get()


@contextmanager
def caller_standing(
    orgs: tuple[uuid.UUID, ...], writable_orgs: tuple[uuid.UUID, ...]
) -> Iterator[None]:
    """Bind the caller's org standing for the block so every `acting_as` inside it reads the orgs.

    The one place standing enters: the MCP identity middleware wraps a tool call in the verified
    token's orgs, and an operator op that legitimately publishes (promote, an operator ingest into
    a shared scope) grants itself exactly the target orgs the same way. `acting_as` reads it back
    through `current_standing`, so a lane's own `acting_as(user_id, lens)` deep in a recall picks
    up the request's standing with nothing threaded through the call tree to carry it.

    orgs: every org the caller reads under for the block.
    writable_orgs: the subset the caller may write into for the block.
    """
    token = _caller_standing.set((orgs, writable_orgs))
    try:
        yield
    finally:
        _caller_standing.reset(token)


def build_engine() -> AsyncEngine:
    """Build the `aizk_app` engine, a real pool by default, NullPool when opted in.

    A pooled connection is safe to reuse across transactions since `events.bind_user` rebinds
    `app.uid`/`app.scopes` transaction-locally on every `after_begin`, so no pooled connection ever
    carries one user's identity into another's transaction. `db_null_pool` exists only for
    the pytest suite, whose many per-test `asyncio.run` loops each need their own fresh connection
    since an asyncpg connection cannot cross event loops. `conftest.py` sets it before any engine
    is built. `pool_pre_ping` stays off since the health-check round trip it adds on every checkout
    is the exact per-call tax pooling exists to remove, and a stale pooled connection fails fast on
    its first real query instead.
    """
    if settings.db_null_pool:
        return create_async_engine(settings.database_url, poolclass=NullPool)
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_pre_ping=False,
    )


@functools.cache
def app_sessions() -> async_sessionmaker[AsyncSession]:
    """The cached `aizk_app` sessionmaker, the role row level security is always enforced under."""
    return async_sessionmaker(build_engine(), expire_on_commit=False)


@functools.cache
def admin_sessions() -> async_sessionmaker[AsyncSession]:
    """The cached `aizk_admin` sessionmaker, the owner role that bypasses row level security.

    `aizk_admin` owns the schema and is not subject to the FORCE-RLS policies `aizk_app` runs
    under, so it is the only connection that can touch the ownerless content tables
    `entity_content`/`fact_content`, which carry no UPDATE policy at all. NullPool under the test
    suite for the same cross-event-loop reason `build_engine` uses one.
    """
    pool = {"poolclass": NullPool} if settings.db_null_pool else {}
    return async_sessionmaker(
        create_async_engine(settings.admin_database_url, **pool), expire_on_commit=False
    )


def session() -> AsyncSession:
    """The open session the enclosing `acting_as`/`bypass_rls` block bound to the task-local
    context.

    Store operations read the session from context here rather than receiving it as a parameter.
    Raises `NoTenantContext` when no block is active, the fail-fast a forgotten `acting_as` earns.
    """
    try:
        return _current_session.get()
    except LookupError:
        raise NoTenantContext("no acting_as session in the current context") from None


@asynccontextmanager
async def bound(opened: AsyncSession) -> AsyncGenerator[AsyncSession]:
    """Bind `opened` to the task-local context for the block so `session()` resolves to it, then
    unbind on exit. One of the two building blocks every session context manager composes from.
    """
    token = _current_session.set(opened)
    try:
        yield opened
    finally:
        _current_session.reset(token)


@asynccontextmanager
async def open_session(
    sessions: async_sessionmaker[AsyncSession], info: dict[str, object]
) -> AsyncGenerator[AsyncSession]:
    """Open a session from `sessions`, stamp its `info`, and bind it to the task-local context.

    The shared opener `acting_as` and `bypass_rls` both compose over, differing only in the role
    sessionmaker they hand it and whether they wrap the yield in a transaction. `events.bind_user`
    reads `info` into the `app.uid`/`app.scopes` GUCs on the first `begin`.

    sessions: the role sessionmaker to open from, `app_sessions()` or `admin_sessions()`.
    info: the `session.info` payload naming the acting user, its org standing, and optional lens.
    """
    async with sessions(info=info) as opened, bound(opened):
        yield opened


@asynccontextmanager
async def acting_as(
    user_id: uuid.UUID, lens: tuple[uuid.UUID, ...] = ()
) -> AsyncGenerator[AsyncSession]:
    """Open an `aizk_app` session whose transaction runs as a given user under row level security.

    The everyday building block: an app-role session, bound to the context, with its transaction
    begun. Stamps the acting user, an optional lens, and the caller's org standing for
    `events.bind_user` to read into the GUCs. The standing is not a parameter: it rides the
    task-local `caller_standing` the enclosing boundary set (empty for a background pass, the
    verified token's orgs for a request), so the same `acting_as(user_id)` a background pass and a
    recall lane both call carries exactly the standing its context established.

    user_id: identity whose visibility the session acts under.
    lens: org ids to narrow reads to (a claim's own set must be contained in this lens and
        non-empty), or an empty tuple for the full visible union with no lens at all.
    """
    orgs, writable_orgs = current_standing()
    async with (
        open_session(
            app_sessions(),
            {"user": user_id, "lens": lens, "orgs": orgs, "writable_orgs": writable_orgs},
        ) as opened,
        opened.begin(),
    ):
        yield opened


def as_system() -> AbstractAsyncContextManager[AsyncSession]:
    """`acting_as` the system user, the background-pass shorthand, still `aizk_app` under RLS.

    Only the identity differs from an ordinary `acting_as`, so a background pass stays inside the
    visibility lattice. When a structural write must reach past every claim's policy, `bypass_rls`
    on the owner role is the deliberate, quarantined alternative instead.
    """
    return acting_as(settings.system_user_id)


@asynccontextmanager
async def bypass_rls() -> AsyncGenerator[AsyncSession]:
    """Open an `aizk_admin` session that bypasses row level security entirely.

    The one place a structural write (entity-dedup merge, RAPTOR rebuild, content re-embed) reaches
    past a claim's own policy to touch content directly, since the app role's own policies forbid
    every UPDATE to content. No auto-`begin`, unlike `acting_as`: the caller owns its transaction
    boundaries, since these passes commit in stages.
    """
    async with open_session(admin_sessions(), {"user": settings.system_user_id}) as opened:
        yield opened
