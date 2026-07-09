from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings
from ..store.context import bound_session


@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    """Open a session on the owner-role admin engine, bypassing row level security entirely.

    The one place a structural write (entity-dedup merge, RAPTOR tree rebuild, content re-embed)
    reaches past a claim's own row-level-security policy to touch content directly, in place of
    each caller hand-rolling its own `create_async_engine`/`dispose` pair around an ad-hoc
    sessionmaker. Disposes the engine when the block exits. The caller owns its own transaction
    boundaries (`session.begin()` once or several times, or an explicit `session.commit()`), since
    callers disagree on how many transactions one admin pass needs.

    Binds the opened session to the task-local context the same way `acting_as` does, so the
    structural writers this pass calls (`repoint_fact_content`, `migrate_entity_claims`,
    `rewrite_embeddings`) reach it through `store.context.session()` rather than a threaded
    argument, resolving to this admin connection while the block is open.
    """
    engine = create_async_engine(settings.admin_database_url)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with (
            sessions(info={"user": settings.system_user_id}) as opened,
            bound_session(opened),
        ):
            yield opened
    finally:
        await engine.dispose()
