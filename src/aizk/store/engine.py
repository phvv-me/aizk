import functools
import uuid
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
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


def build_engine() -> AsyncEngine:
    """Build the app-role async engine, a real pool by default, NullPool when opted in.

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
def async_session() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(build_engine(), expire_on_commit=False)


_current_session: ContextVar[AsyncSession] = ContextVar("aizk_session")


def session() -> AsyncSession:
    """The open session the enclosing `acting_as`/`admin_session` block bound to the task-local
    context.

    Store operations read the session from context here rather than receiving it as a parameter.
    Raises `NoTenantContext` when no block is active, the fail-fast a forgotten `acting_as` earns.
    """
    try:
        return _current_session.get()
    except LookupError:
        raise NoTenantContext("no acting_as session in the current context") from None


@asynccontextmanager
async def acting_as(
    user_id: uuid.UUID, scopes: tuple[uuid.UUID, ...] = ()
) -> AsyncGenerator[AsyncSession]:
    """Open a session whose transaction runs as a given user under row level security.

    Stamps `session.info` with the acting user and optional lens, which `events.bind_user` reads
    into the app.uid and app.scopes GUCs, and binds the open session to the task-local context so
    `session()` reaches it without a threaded parameter.

    user_id: identity whose visibility the session acts under.
    scopes: group ids to narrow reads to (a claim's own set must be contained in this lens and
        non-empty), or an empty tuple for the full visible union with no lens at all.
    """
    async with async_session()(info={"user": user_id, "lens": scopes}) as opened, opened.begin():
        token = _current_session.set(opened)
        try:
            yield opened
        finally:
            _current_session.reset(token)


def system_session() -> AbstractAsyncContextManager[AsyncSession]:
    """Open a session acting as `settings.system_user_id`, the background-pass shorthand.

    Still the app role under row level security, only the identity differs, so a background pass
    stays inside the visibility lattice. When a structural write must reach past every claim's own
    policy, `admin_session` on the owner role is the deliberate, quarantined break-glass instead.
    """
    return acting_as(settings.system_user_id)


@asynccontextmanager
async def admin_session() -> AsyncGenerator[AsyncSession]:
    """Open a session on the owner-role admin engine, bypassing row level security entirely.

    The one place a structural write (entity-dedup merge, RAPTOR rebuild, content re-embed) reaches
    past a claim's own policy to touch content directly. Disposes its throwaway engine on exit, and
    binds the session to the task-local context the same way `acting_as` does so `session()`
    resolves inside the block. The caller owns its own transaction boundaries.
    """
    engine = create_async_engine(settings.admin_database_url)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions(info={"user": settings.system_user_id}) as opened:
            token = _current_session.set(opened)
            try:
                yield opened
            finally:
                _current_session.reset(token)
    finally:
        await engine.dispose()
