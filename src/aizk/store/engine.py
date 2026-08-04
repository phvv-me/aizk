from contextlib import AsyncExitStack
from enum import StrEnum, auto
from functools import cache
from types import TracebackType
from typing import Self

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
)
from sqlalchemy.orm import Session as OrmSession
from sqlmodel.ext.asyncio.session import AsyncSession

from ..config import settings
from ..exceptions import NoTenantContext
from .backend import bind_cockroach_authority, database_adapter
from .identity import User

event.listen(OrmSession, "after_begin", bind_cockroach_authority)


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
            database_adapter().configure_session(opened.sync_session, self.user)
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
        app_role = self.role is DatabaseRole.app
        url = settings.database_url if app_role else settings.admin_database_url
        return database_adapter().engine(url, app_role)

    def session(self, user: User) -> SessionScope:
        """Open a caller-bound session for sequential transaction scopes."""
        return SessionScope(self._sessions, user, transactional=False)

    def transaction(self, user: User) -> SessionScope:
        """Run one transaction as the given caller under this database role."""
        return SessionScope(self._sessions, user, transactional=True)
