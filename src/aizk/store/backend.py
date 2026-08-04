import abc
import ssl
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import UUID5
from sqlalchemy import NullPool, bindparam, func
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import SessionTransaction
from sqlmodel import select

from ..config import DatabaseBackend, settings

if TYPE_CHECKING:
    from .identity import User

_AUTHORITY_INFO = "aizk.cockroach_authority"


class DatabaseAdapter(abc.ABC):
    """Own the database-specific engine and transaction context behavior."""

    @abc.abstractmethod
    def engine(self, url: str | URL, app_role: bool) -> AsyncEngine:
        """Build one role-specific asynchronous engine."""

    @abc.abstractmethod
    def configure_session(self, session: OrmSession, user: User) -> None:
        """Attach caller authority for every transaction opened by `session`."""

    @staticmethod
    def pooled_engine(
        url: str | URL,
        app_role: bool,
        ssl_config: ssl.SSLContext | bool | str | None = None,
    ) -> AsyncEngine:
        """Build the shared pool shape without database-specific connection settings."""
        connect_args = {} if ssl_config is None else {"ssl": ssl_config}
        if settings.db_null_pool:
            return create_async_engine(url, connect_args=connect_args, poolclass=NullPool)
        if not app_role:
            return create_async_engine(url, connect_args=connect_args)
        return create_async_engine(
            url,
            connect_args=connect_args,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_max_overflow,
            pool_pre_ping=False,
        )


class PostgreSQLAdapter(DatabaseAdapter):
    """Preserve VectorChord and custom-setting behavior for PostgreSQL."""

    def engine(self, url: str | URL, app_role: bool) -> AsyncEngine:
        if not app_role or settings.db_null_pool:
            return self.pooled_engine(url, app_role)
        return create_async_engine(
            url,
            connect_args={"server_settings": {"vchordrq.prefilter": "on"}},
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_max_overflow,
            pool_pre_ping=False,
        )

    def configure_session(self, session: OrmSession, user: User) -> None:
        session.info.update(user.info())


class CockroachDBAdapter(DatabaseAdapter):
    """Carry RLS authority through CockroachDB's supported `application_name` setting."""

    def engine(self, url: str | URL, app_role: bool) -> AsyncEngine:
        normalized, ssl_config = self.cloud_connection(url)
        return self.pooled_engine(normalized, app_role, ssl_config)

    @staticmethod
    def cloud_connection(
        url: str | URL,
    ) -> tuple[URL, ssl.SSLContext | bool | str | None]:
        """Translate a `ccloud` libpq URL into asyncpg TLS settings."""
        parsed = make_url(url)
        query = dict(parsed.query)
        mode = query.pop("sslmode", None)
        root_certificate = query.pop("sslrootcert", None)
        if mode is None:
            return parsed, None
        normalized = parsed.set(query=query)
        if mode == "disable":
            return normalized, False
        if mode == "require":
            return normalized, mode
        if mode not in {"verify-ca", "verify-full"}:
            raise ValueError(f"unsupported CockroachDB sslmode {mode}")
        context = ssl.create_default_context()
        if settings.db_ssl_root_certificate:
            context.load_verify_locations(cadata=settings.db_ssl_root_certificate)
        elif isinstance(root_certificate, str):
            certificate_path = Path(root_certificate).expanduser()
            if certificate_path.is_file():
                context.load_verify_locations(cadata=certificate_path.read_text())
        context.check_hostname = mode == "verify-full"
        return normalized, context

    def configure_session(self, session: OrmSession, user: User) -> None:
        def array(permission: frozenset[UUID5]) -> str:
            return "{" + ",".join(str(scope) for scope in sorted(permission)) + "}"

        session.info[_AUTHORITY_INFO] = "|".join(
            (
                "aizk",
                array(user.scopes.read),
                array(user.scopes.write),
                array(user.scopes.public),
            )
        )


def database_adapter() -> DatabaseAdapter:
    """Build the configured database strategy."""
    match settings.database_backend:
        case DatabaseBackend.postgresql:
            return PostgreSQLAdapter()
        case DatabaseBackend.cockroachdb:
            return CockroachDBAdapter()
        case _:
            raise ValueError(f"unsupported database backend {settings.database_backend}")


def bind_cockroach_authority(
    session: OrmSession,
    transaction: SessionTransaction,
    connection: Connection,
) -> None:
    """Bind one transaction-local CockroachDB RLS authority document."""
    del transaction
    authority = session.info.get(_AUTHORITY_INFO)
    if not isinstance(authority, str):
        return
    connection.execute(
        select(
            func.set_config(
                "application_name",
                bindparam("aizk_cockroach_authority"),
                True,
            )
        ),
        {"aizk_cockroach_authority": authority},
    )
