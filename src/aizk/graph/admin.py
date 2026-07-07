from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings


@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    """Open a session on the owner-role admin engine, bypassing row level security entirely.

    The one place a structural write (entity-dedup merge, RAPTOR tree rebuild, content re-embed)
    reaches past a claim's own row-level-security policy to touch content directly, in place of
    each caller hand-rolling its own `create_async_engine`/`dispose` pair around an ad-hoc
    sessionmaker. Disposes the engine when the block exits. The caller owns its own transaction
    boundaries (`session.begin()` once or several times, or an explicit `session.commit()`), since
    callers disagree on how many transactions one admin pass needs.
    """
    engine = create_async_engine(settings.admin_database_url)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions(info={"principal": settings.system_user_id}) as session:
            yield session
    finally:
        await engine.dispose()
