import asyncio
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from patos import FrozenModel
from sqlalchemy.engine import make_url

from .config import settings


class BackupReport(FrozenModel):
    """The result of one `pg_dump`, the whole database captured as a single portable file.

    path: the archive written, a `pg_dump` custom-format file `restore` reads back.
    bytes: the archive's size on disk, so a caller sees a backup actually landed something.
    """

    path: str
    bytes: int


class RestoreReport(FrozenModel):
    """The result of one `pg_restore`, an archive loaded back into a database.

    path: the archive read.
    database: the database the archive was restored into.
    """

    path: str
    database: str


class BackupError(RuntimeError):
    """A `pg_dump` or `pg_restore` subprocess exited non-zero, its stderr carried here so the
    failure reads at the call site rather than as a bare exit code. A version mismatch, the client
    older than the server, surfaces here too, the reason `settings.pg_client_launcher` exists."""


def connection_url(database: str | None = None) -> str:
    """The owner-role libpq URL the backup tools connect with, an alternate database swapped in.

    Defaults to `settings.admin_asyncpg_dsn` so a dump reads every row past row level security and
    a restore recreates every object, overridable through `settings.backup_database_url` for the
    common case where the tools run inside the database container and reach Postgres on its own
    internal address rather than the host-mapped port.

    database: a database name to point at instead of the configured one, the seam a
        disaster-recovery or a scratch-database restore uses.
    """
    url = make_url(settings.backup_database_url or settings.admin_asyncpg_dsn)
    if database is not None:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False)


async def run_pg_tool(
    args: list[str], *, stdout_path: str | None = None, stdin_path: str | None = None
) -> None:
    """Run one `pg_dump`/`pg_restore` invocation, streaming the archive to or from a host file.

    The archive rides over the process's own stdout or stdin rather than a `--file` argument, so
    the tool runs anywhere the launcher puts it, the host or inside the database container, while
    the archive always lands on the host that invoked the backup. Streaming to a file descriptor
    keeps a large dump off the Python heap. Raises `BackupError` with the tool's stderr on a
    non-zero exit.

    args: the full command line, the launcher prefix already prepended, the tool name next.
    stdout_path: a host file the tool's stdout is written to, the dump archive for a backup.
    stdin_path: a host file fed to the tool's stdin, the archive for a restore.
    """
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
    """Dump the whole database to a portable `pg_dump` custom-format archive at `path`.

    The custom format is compressed and `pg_restore` reads it selectively, so one file is the
    complete, portable backup. The archive carries the schema, the seeded and grown ontology,
    every tenant's rows, the bi-temporal history, and the app-role grants, so a restore onto a
    fresh instance whose `roles.sh` already minted the app role comes up ready.

    path: the host file the archive is written to.
    """
    await run_pg_tool(
        [*settings.pg_client_launcher, "pg_dump", "--format=custom", "--dbname", connection_url()],
        stdout_path=path,
    )
    return BackupReport(path=path, bytes=Path(path).stat().st_size)


async def restore_database(path: str, database: str | None = None) -> RestoreReport:
    """Load a `pg_dump` archive back into a database, the counterpart to `backup_database`.

    Destructive on the configured database, `--clean --if-exists` drops each object the archive
    recreates before loading it, so the target ends carrying exactly the backup's contents.
    Passing `database` points the restore at a fresh, empty database instead and drops the clean
    flags, the non-destructive path a recovery drill or a proven-restore test uses. Runs as the
    owner, a superuser, so the archive's `CREATE EXTENSION` lines and app-role grants both apply,
    and forced row level security comes back exactly as the backup held it.

    path: the archive to read.
    database: an alternate, already-created database to restore into, the configured database
        when null.
    """
    conn = connection_url(database)
    clean = [] if database is not None else ["--clean", "--if-exists"]
    await run_pg_tool(
        [*settings.pg_client_launcher, "pg_restore", *clean, "--dbname", conn], stdin_path=path
    )
    return RestoreReport(path=path, database=make_url(conn).database or settings.db_name)


def prune_backups(directory: Path, keep_days: int) -> int:
    """Delete every dump under `directory` older than `keep_days`, returning how many were removed.

    Keyed on each file's own modification time and the `aizk-*.dump` name `scheduled_backup`
    writes, so a hand-placed file the pattern does not match is never touched.

    directory: the backup directory to prune.
    keep_days: age past which a dump is deleted.
    """
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    removed = 0
    for dump in directory.glob("aizk-*.dump"):
        if datetime.fromtimestamp(dump.stat().st_mtime, UTC) < cutoff:
            dump.unlink()
            removed += 1
    return removed


async def scheduled_backup() -> BackupReport:
    """One scheduled backup, a timestamped dump under `settings.backup_dir` with old ones pruned.

    The body the `BackupTask` cron runs, so the same durable pgqueuer scheduler that fires decay,
    dedup, and the rest fires the backup too, no separate service or cron of its own. Creates the
    directory on first run, names the dump by UTC timestamp so a day's dumps never collide, and
    prunes anything past `settings.backup_keep_days` so the disk does not grow without bound.
    """
    directory = Path(settings.backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = await backup_database(str(directory / f"aizk-{stamp}.dump"))
    prune_backups(directory, settings.backup_keep_days)
    return report
