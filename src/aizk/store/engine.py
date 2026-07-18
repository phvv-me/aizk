from contextlib import AsyncExitStack
from enum import StrEnum, auto
from functools import cache
from types import TracebackType
from typing import Self

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from ..config import settings
from ..exceptions import NoTenantContext
from .identity import User


class Session(AsyncSession):
    """Expose the Aizk caller bound to one SQLModel session and its RLS settings."""

    @property
    def user(self) -> User:
        """Return this transaction's caller."""
        user = self.info.get("user")
        if not isinstance(user, User):
            raise NoTenantContext("database session has no user")
        return user


class DatabaseRole(StrEnum):
    """Choose forced tenant isolation or RLS-bypassing schema-owner maintenance."""

    app = auto()
    owner = auto()


class SessionScope:
    """Own the complete lifetime of one caller-bound async session and transaction."""

    __slots__ = ("factory", "stack", "transactional", "user")

    def __init__(
        self,
        factory: async_sessionmaker[Session],
        user: User,
        transactional: bool,
    ) -> None:
        self.factory = factory
        self.user = user
        self.transactional = transactional
        self.stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Session:
        """Open the session and begin its transaction when requested."""
        if self.stack is not None:
            raise RuntimeError("session scope is already open")
        async with AsyncExitStack() as opening:
            opened = await opening.enter_async_context(self.factory())
            opened.info["user"] = self.user
            opened.info.update(self.user.info())
            if self.transactional:
                await opening.enter_async_context(opened.begin())
            self.stack = opening.pop_all()
            return opened

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Commit or roll back the transaction and close the session."""
        stack = self.stack
        if stack is None:
            raise RuntimeError("session scope is not open")
        self.stack = None
        return await stack.__aexit__(exc_type, exc, traceback)


class Database:
    """Cache one SQLAlchemy engine and typed session factory per PostgreSQL role."""

    __slots__ = ("_sessions", "engine", "role")

    def __init__(self, role: DatabaseRole) -> None:
        self.role = role
        self.engine = self._build_engine()
        self._sessions = async_sessionmaker(
            self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    # Kept as the one process-wide singleton pair by design. Every `User` session and
    # transaction resolves its engine here, so injecting it would thread a Runtime through
    # nearly every query call site for no isolation gain, while the connection pool must
    # stay per-process anyway.
    @classmethod
    @cache
    def app(cls) -> Self:
        """Return the restricted app engine that can access tenant rows only through RLS."""
        return cls(DatabaseRole.app)

    @classmethod
    @cache
    def owner(cls) -> Self:
        """Return the privileged migration and maintenance engine that bypasses RLS."""
        return cls(DatabaseRole.owner)

    def _build_engine(self) -> AsyncEngine:
        """Build this role's async engine and connection pool."""
        if self.role is DatabaseRole.owner:
            if settings.db_null_pool:
                return create_async_engine(settings.admin_database_url, poolclass=NullPool)
            return create_async_engine(settings.admin_database_url)
        if settings.db_null_pool:
            return create_async_engine(
                settings.database_url,
                poolclass=NullPool,
                connect_args={"server_settings": {"vchordrq.prefilter": "on"}},
            )
        return create_async_engine(
            settings.database_url,
            connect_args={"server_settings": {"vchordrq.prefilter": "on"}},
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_max_overflow,
            pool_pre_ping=False,
        )

    def session(self, user: User) -> SessionScope:
        """Open a caller-bound session for sequential transaction scopes."""
        return SessionScope(self._sessions, user, transactional=False)

    def transaction(self, user: User) -> SessionScope:
        """Run one transaction as the given caller under this database role."""
        return SessionScope(self._sessions, user, transactional=True)
