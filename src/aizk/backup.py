import asyncio
import os
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from patos import FrozenModel
from pydantic import SecretStr
from sqlalchemy import func
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import URL, make_url
from sqlalchemy.sql import ClauseElement
from sqlmodel import select

from .config import settings


class BackupReport(FrozenModel):
    """The result of one `pg_dump`, the whole database captured as a single portable file."""

    path: str
    bytes: int


class RestoreReport(FrozenModel):
    """The result of one `pg_restore`, an archive loaded back into a database."""

    path: str
    database: str


class BackupError(RuntimeError):
    """A `pg_dump` or `pg_restore` subprocess exited non-zero, its stderr carried here so the
    failure reads at the call site rather than as a bare exit code."""


class BackupConnection(FrozenModel):
    """Password-separated libpq connection for PostgreSQL backup subprocesses.

    `url` is safe to place in a process argument and diagnostic output because it
    contains no password. `environment` supplies the password through `PGPASSWORD`,
    which keeps the secret out of process command lines.
    """

    url: str
    database: str
    password: SecretStr

    @classmethod
    def configured(cls, database: str | None = None) -> BackupConnection:
        """Build the configured owner connection, optionally targeting another database."""
        source = make_url(settings.backup_database_url or settings.admin_asyncpg_dsn)
        target = source.set(database=database) if database is not None else source
        public = URL.create(
            drivername=target.drivername,
            username=target.username,
            host=target.host,
            port=target.port,
            database=target.database,
            query=target.query,
        )
        return cls(
            url=public.render_as_string(hide_password=False),
            database=target.database or settings.db_name,
            password=SecretStr(target.password or ""),
        )

    @property
    def environment(self) -> dict[str, str]:
        """Return the inherited process environment with this connection password added."""
        return {**os.environ, "PGPASSWORD": self.password.get_secret_value()}


async def run_pg_tool(
    args: list[str],
    *,
    environment: dict[str, str] | None = None,
    stdout_path: str | None = None,
    stdin_path: str | None = None,
) -> None:
    """Run one PostgreSQL client while streaming archives without exposing credentials.

    args: complete client command whose arguments contain no database password.
    environment: optional subprocess environment carrying `PGPASSWORD`.
    stdout_path: optional archive destination created with owner-only permissions.
    stdin_path: optional archive source streamed directly to the client.
    """
    with ExitStack() as handles:
        stdout: BinaryIO | None = None
        stdin: BinaryIO | None = None
        if stdout_path:
            descriptor = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(descriptor, 0o600)
            stdout = handles.enter_context(open(descriptor, "wb"))
        if stdin_path:
            stdin = handles.enter_context(open(stdin_path, "rb"))
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=stdin,
            stdout=stdout,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        _, stderr = await process.communicate()
    if process.returncode != 0:
        raise BackupError(f"{args[0]} exited {process.returncode}: {stderr.decode().strip()}")


async def backup_database(path: str) -> BackupReport:
    """Write one owner-only custom-format archive containing the complete Aizk database."""
    connection = BackupConnection.configured()
    await run_pg_tool(
        ["pg_dump", "--format=custom", "--dbname", connection.url],
        environment=connection.environment,
        stdout_path=path,
    )
    return BackupReport(path=path, bytes=Path(path).stat().st_size)


async def restore_database(path: str, database: str | None = None) -> RestoreReport:
    """Restore a complete archive, replacing live objects unless a scratch database is named."""
    connection = BackupConnection.configured(database)
    clean = [] if database is not None else ["--clean", "--if-exists"]
    await run_pg_tool(
        [
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            *clean,
            "--dbname",
            connection.url,
        ],
        environment=connection.environment,
        stdin_path=path,
    )
    await ensure_bm25_tokenizer(database)
    return RestoreReport(path=path, database=connection.database)


def psql_sql(statement: ClauseElement) -> str:
    """Compile one bound SQLAlchemy statement into literal PostgreSQL for the `psql`
    boundary."""
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


async def ensure_bm25_tokenizer(database: str | None = None) -> None:
    """Re-register the BM25 tokenizer when a restore left the database without it."""
    connection = BackupConnection.configured(database)
    psql = [
        "psql",
        connection.url,
        "-v",
        "ON_ERROR_STOP=1",
        "-qAt",
        "-c",
    ]
    probe = select(func.tokenizer_catalog.tokenize("probe", "aizk_bm25"))
    create = select(func.tokenizer_catalog.create_tokenizer("aizk_bm25", 'model = "llmlingua2"'))
    try:
        await run_pg_tool([*psql, psql_sql(probe)], environment=connection.environment)
    except BackupError:
        await run_pg_tool([*psql, psql_sql(create)], environment=connection.environment)


def prune_backups(directory: Path, keep_days: int) -> int:
    """Delete every dump under `directory` older than `keep_days`, returning how many were
    removed."""
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    removed = 0
    for dump in directory.glob("aizk-*.dump"):
        if datetime.fromtimestamp(dump.stat().st_mtime, UTC) < cutoff:
            dump.unlink()
            removed += 1
    return removed


async def scheduled_backup() -> BackupReport:
    """One scheduled backup, a timestamped dump under `settings.backup_dir` with old ones
    pruned."""
    directory = Path(settings.backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = await backup_database(str(directory / f"aizk-{stamp}.dump"))
    prune_backups(directory, settings.backup_keep_days)
    return report
