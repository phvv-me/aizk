import asyncio
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from patos import FrozenModel
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.sql import ClauseElement

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


def connection_url(database: str | None = None) -> str:
    """The owner-role libpq URL the backup tools connect with, an alternate database swapped
    in."""
    url = make_url(settings.backup_database_url or settings.admin_asyncpg_dsn)
    if database is not None:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False)


async def run_pg_tool(
    args: list[str], *, stdout_path: str | None = None, stdin_path: str | None = None
) -> None:
    """Run one `pg_dump`/`pg_restore` invocation, streaming the archive to or from a host
    file."""
    with ExitStack() as handles:
        stdout: BinaryIO | None = None
        stdin: BinaryIO | None = None
        if stdout_path:
            stdout = handles.enter_context(open(stdout_path, "wb"))
        if stdin_path:
            stdin = handles.enter_context(open(stdin_path, "rb"))
        process = await asyncio.create_subprocess_exec(
            *args, stdin=stdin, stdout=stdout, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
    if process.returncode != 0:
        tool = args[len(settings.pg_client_launcher)]
        raise BackupError(f"{tool} exited {process.returncode}: {stderr.decode().strip()}")


async def backup_database(path: str) -> BackupReport:
    """Dump the whole database to a portable `pg_dump` custom-format archive at `path`."""
    await run_pg_tool(
        [*settings.pg_client_launcher, "pg_dump", "--format=custom", "--dbname", connection_url()],
        stdout_path=path,
    )
    return BackupReport(path=path, bytes=Path(path).stat().st_size)


async def restore_database(path: str, database: str | None = None) -> RestoreReport:
    """Load a `pg_dump` archive back into a database, the counterpart to `backup_database`."""
    conn = connection_url(database)
    clean = [] if database is not None else ["--clean", "--if-exists"]
    await run_pg_tool(
        [*settings.pg_client_launcher, "pg_restore", *clean, "--dbname", conn], stdin_path=path
    )
    await ensure_bm25_tokenizer(database)
    return RestoreReport(path=path, database=make_url(conn).database or settings.db_name)


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
    url = connection_url(database)
    psql = [*settings.pg_client_launcher, "psql", url, "-v", "ON_ERROR_STOP=1", "-qAt", "-c"]
    probe = select(func.tokenizer_catalog.tokenize("probe", "aizk_bm25"))
    create = select(func.tokenizer_catalog.create_tokenizer("aizk_bm25", 'model = "llmlingua2"'))
    try:
        await run_pg_tool([*psql, psql_sql(probe)])
    except BackupError:
        await run_pg_tool([*psql, psql_sql(create)])


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
