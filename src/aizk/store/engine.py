import functools

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import settings


def build_engine() -> AsyncEngine:
    """Build the app-role async engine, a real pool by default, NullPool when opted in.

    A pooled connection is safe to reuse across transactions since `events.bind_principal` rebinds
    `app.uid`/`app.scopes` transaction-locally on every `after_begin`, so no pooled connection ever
    carries one principal's identity into another's transaction. `db_null_pool` exists only for
    the pytest suite, whose many per-test `asyncio.run` loops each need their own fresh connection
    since an asyncpg connection cannot cross event loops; `conftest.py` sets it before any engine
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
