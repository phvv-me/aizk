import uuid
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .engine import async_session


@asynccontextmanager
async def acting_as(
    principal_id: uuid.UUID, scopes: tuple[uuid.UUID, ...] = ()
) -> AsyncGenerator[AsyncSession]:
    """Open a session whose transaction runs as a given principal under row level security.

    Stamps `session.info` with the acting principal and the optional scope-set reading lens right
    at construction, `{"principal": principal_id, "lens": scopes}`, the identity
    `events.bind_principal`'s `after_begin` listener reads back to bind the app.uid and app.scopes
    GUCs. The session carries its own acting identity this way rather than through a ContextVar
    threaded around it, so every read and write the block issues, however many sessions it opens,
    is scoped to this principal, narrowed to the lens `scopes` projects when one is given.

    principal_id: identity whose visibility the session acts under.
    scopes: group ids to narrow reads to (a claim's own set must be contained in this lens and
        non-empty), or an empty tuple for the full visible union with no lens at all.
    """
    async with (
        async_session()(info={"principal": principal_id, "lens": scopes}) as session,
        session.begin(),
    ):
        yield session


def system_session() -> AbstractAsyncContextManager[AsyncSession]:
    """Open a session acting as `settings.system_principal_id`, the background-pass shorthand.

    Collapses the common `acting_as(settings.system_principal_id)` pairing callers otherwise
    repeat at every scheduled pass and identity-table write into one call.
    """
    return acting_as(settings.system_principal_id)
