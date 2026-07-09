import uuid
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..exceptions import NoTenantContext
from .engine import async_session

_current_session: ContextVar[AsyncSession] = ContextVar("aizk_session")


def session() -> AsyncSession:
    """The open session the enclosing `acting_as` block bound to the task-local context.

    Store operations run inside an `acting_as` (or `admin_session`) block that binds the open
    transaction; reading the session from context here lets them drop the `session` parameter they
    once threaded through every call. Raises `NoTenantContext` when no block is active.
    """
    try:
        return _current_session.get()
    except LookupError:
        raise NoTenantContext("no acting_as session in the current context") from None


@asynccontextmanager
async def bound_session(active: AsyncSession) -> AsyncGenerator[AsyncSession]:
    """Bind an already-open session to the task-local context so `session()` reaches it.

    `acting_as` binds its app-engine session through here; `admin_session` (a separate owner-role
    engine that bypasses row level security) binds its own the same way, so `session()` resolves
    inside an admin block too.
    """
    token = _current_session.set(active)
    try:
        yield active
    finally:
        _current_session.reset(token)


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
    async with (
        async_session()(info={"user": user_id, "lens": scopes}) as opened,
        opened.begin(),
        bound_session(opened),
    ):
        yield opened


def system_session() -> AbstractAsyncContextManager[AsyncSession]:
    """Open a session acting as `settings.system_user_id`, the background-pass shorthand.

    Collapses the common `acting_as(settings.system_user_id)` pairing callers otherwise repeat at
    every scheduled pass and identity-table write into one call.
    """
    return acting_as(settings.system_user_id)
