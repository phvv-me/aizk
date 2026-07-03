import functools

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings


@functools.cache
def async_session() -> async_sessionmaker[AsyncSession]:
    instance = create_async_engine(settings.database_url, poolclass=NullPool)
    return async_sessionmaker(instance, expire_on_commit=False)
