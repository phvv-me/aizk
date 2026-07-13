from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from ..config import settings
from ..exceptions import NoTenantContext
from .identity import User


class Session(AsyncSession):
    """SQLModel session carrying the caller whose authority PostgreSQL enforces."""

    @property
    def user(self) -> User:
        """Return this transaction's caller."""
        user = self.info.get("user")
        if not isinstance(user, User):
            raise NoTenantContext("database session has no user")
        return user


def build_engine(admin: bool = False) -> AsyncEngine:
    """Build an engine for the app or owner database role."""
    url = settings.admin_database_url if admin else settings.database_url
    if admin:
        return (
            create_async_engine(url, poolclass=NullPool)
            if settings.db_null_pool
            else create_async_engine(url)
        )
    if settings.db_null_pool:
        return create_async_engine(
            url,
            poolclass=NullPool,
            connect_args={"server_settings": {"vchordrq.prefilter": "on"}},
        )
    return create_async_engine(
        url,
        connect_args={"server_settings": {"vchordrq.prefilter": "on"}},
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_pre_ping=False,
    )


@cache
def session_factory(admin: bool = False) -> async_sessionmaker[Session]:
    """Reuse one typed session factory and connection pool per database role."""
    return async_sessionmaker(
        build_engine(admin),
        class_=Session,
        expire_on_commit=False,
    )


@asynccontextmanager
async def session_for(user: User) -> AsyncIterator[Session]:
    """Open one caller-bound session whose transactions may be short and sequential."""
    async with session_factory()() as opened:
        opened.info["user"] = user
        opened.info.update(user.info())
        yield opened


@asynccontextmanager
async def transaction(user: User) -> AsyncIterator[Session]:
    """Run one short app-role transaction as the given caller."""
    async with session_for(user) as opened, opened.begin():
        yield opened


def as_system() -> User:
    """Run one app-role transaction as the service identity."""
    return User.system()


@asynccontextmanager
async def bypass_rls() -> AsyncIterator[Session]:
    """Run one owner-role transaction for structural maintenance."""
    async with session_factory(admin=True).begin() as opened:
        opened.info["user"] = User.system()
        yield opened
