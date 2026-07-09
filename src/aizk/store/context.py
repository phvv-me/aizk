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

    Store operations run inside an `acting_as` (or `system_session`) block that opens the
    transaction and binds the acting identity; reading the session from context here lets them drop
    the `session` parameter they once threaded through every call. Raises `NoTenantContext` when no
    block is active, the same fail-fast a forgotten `acting_as` already earns.
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
    into the app.uid and app.scopes GUCs, and binds the session to the task-local context so
    `session()` reaches it without a threaded parameter.

    user_id: identity whose visibility the session acts under.
    scopes: group ids to narrow reads to (a claim's own set must be contained in this lens and
        non-empty), or an empty tuple for the full visible union with no lens at all.
    """
    async with (
        async_session()(info={"user": user_id, "lens": scopes}) as opened,
        opened.begin(),
    ):
        token = _current_session.set(opened)
        try:
            yield opened
        finally:
            _current_session.reset(token)


def system_session() -> AbstractAsyncContextManager[AsyncSession]:
    """Open a session acting as `settings.system_user_id`, the background-pass shorthand.

    Collapses the common `acting_as(settings.system_user_id)` pairing callers otherwise repeat at
    every scheduled pass and identity-table write into one call.
    """
    return acting_as(settings.system_user_id)
