import asyncio
from pathlib import Path
from typing import BinaryIO

import pytest

from aizk import backup
from aizk.backup import BackupError
from aizk.config import settings


class FakeProcess:
    """A `create_subprocess_exec` stand-in's return, only `returncode` and `communicate` read.

    returncode: the exit code the fake tool reports.
    stderr: the bytes `communicate` hands back as the tool's stderr.
    """

    def __init__(self, returncode: int, stderr: bytes) -> None:
        self.returncode = returncode
        self.stderr = stderr

    async def communicate(self) -> tuple[None, bytes]:
        """Return the fixed stdout and stderr, stdout None since it streamed straight to a file."""
        return None, self.stderr


def patch_pg_tool(
    monkeypatch: pytest.MonkeyPatch,
    record: dict[str, object],
    *,
    returncode: int = 0,
    reported_stderr: bytes = b"",
    archive: bytes = b"",
) -> None:
    """Replace the one subprocess seam with a fake recording its argv and streaming `archive`.

    The fake writes `archive` to whatever file handle `backup_database` opened for stdout, so a
    backup's reported size is real without ever running `pg_dump`, and records the argv and the
    stdin/stdout handles so a test asserts the exact command the tools were invoked with.

    record: a dict the fake populates with `args`, `stdin`, and `stdout`.
    returncode: the exit code the fake tool reports.
    reported_stderr: the stderr bytes the fake tool reports, named apart from the `stderr`
        redirect argument the real call passes so the two never collide.
    archive: bytes the fake writes to the stdout handle, a backup's simulated dump.
    """

    async def fake(
        *args: str,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
        stderr: int | None = None,
    ) -> FakeProcess:
        record["args"] = list(args)
        record["stdin"] = stdin
        record["stdout"] = stdout
        if stdout is not None and archive:
            stdout.write(archive)
        return FakeProcess(returncode, reported_stderr)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)


def test_backup_streams_pg_dump_to_the_path_and_reports_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup runs `pg_dump --format=custom` streaming to the file and reports its real size."""
    monkeypatch.setattr(settings, "pg_client_launcher", [])
    record: dict[str, object] = {}
    patch_pg_tool(monkeypatch, record, archive=b"ARCHIVE")
    dump = tmp_path / "x.dump"

    report = asyncio.run(backup.backup_database(str(dump)))

    assert report.bytes == len(b"ARCHIVE")
    assert record["args"][:3] == ["pg_dump", "--format=custom", "--dbname"]  # type: ignore[index]
    assert record["stdout"] is not None  # streamed to the file handle, not captured in memory


def test_restore_streams_the_archive_and_cleans_the_configured_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring the configured database runs `pg_restore --clean --if-exists` reading the file."""
    monkeypatch.setattr(settings, "pg_client_launcher", [])
    record: dict[str, object] = {}
    patch_pg_tool(monkeypatch, record)
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"ARCHIVE")

    report = asyncio.run(backup.restore_database(str(dump)))

    assert record["args"][0] == "pg_restore"  # type: ignore[index]
    assert "--clean" in record["args"] and "--if-exists" in record["args"]  # type: ignore[operator]
    assert record["stdin"] is not None
    assert report.database == settings.db_name


def test_restore_into_a_scratch_database_skips_the_clean_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring into a named fresh database drops the clean flags, the non-destructive path."""
    monkeypatch.setattr(settings, "pg_client_launcher", [])
    record: dict[str, object] = {}
    patch_pg_tool(monkeypatch, record)
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"ARCHIVE")

    report = asyncio.run(backup.restore_database(str(dump), database="scratch"))

    assert "--clean" not in record["args"]  # type: ignore[operator]
    assert report.database == "scratch"


def test_a_nonzero_exit_raises_backup_error_naming_the_tool_and_its_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool exiting non-zero, a version mismatch among the causes, raises a legible error."""
    monkeypatch.setattr(settings, "pg_client_launcher", [])
    patch_pg_tool(
        monkeypatch, {}, returncode=1, reported_stderr=b"aborting because of server version"
    )

    with pytest.raises(BackupError, match="pg_dump exited 1.*server version"):
        asyncio.run(backup.backup_database(str(tmp_path / "x.dump")))


def test_the_launcher_prefixes_the_command_so_the_tool_runs_in_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured launcher runs the tool through it, the compose version-match seam."""
    monkeypatch.setattr(settings, "pg_client_launcher", ["docker", "exec", "-i", "aizk-db-1"])
    record: dict[str, object] = {}
    patch_pg_tool(monkeypatch, record, archive=b"A")

    asyncio.run(backup.backup_database(str(tmp_path / "x.dump")))

    assert record["args"][:5] == ["docker", "exec", "-i", "aizk-db-1", "pg_dump"]  # type: ignore[index]


def test_connection_url_prefers_the_override_and_swaps_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backup connection uses `backup_database_url` when set, and swaps in an alternate db."""
    monkeypatch.setattr(settings, "backup_database_url", "postgresql://aizk:pw@db:5432/aizk")
    assert "db:5432/aizk" in backup.connection_url()
    assert backup.connection_url("scratch").endswith("/scratch")

    monkeypatch.setattr(settings, "backup_database_url", "")
    assert backup.connection_url() == settings.admin_asyncpg_dsn


def test_prune_backups_removes_only_the_old_matching_dumps(tmp_path: Path) -> None:
    """Prune deletes dumps past the age cutoff, leaving recent and non-matching files alone."""
    import os
    import time

    old = tmp_path / "aizk-old.dump"
    recent = tmp_path / "aizk-recent.dump"
    other = tmp_path / "notes.txt"
    for path in (old, recent, other):
        path.write_bytes(b"x")
    old_time = time.time() - 30 * 86400  # 30 days ago, past a 14-day keep
    os.utime(old, (old_time, old_time))

    removed = backup.prune_backups(tmp_path, keep_days=14)

    assert removed == 1
    assert not old.exists()
    assert recent.exists() and other.exists()  # recent kept, non-matching name untouched


def test_scheduled_backup_writes_a_timestamped_dump_and_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled backup dumps into `backup_dir` under a timestamped name and prunes old dumps."""
    monkeypatch.setattr(settings, "pg_client_launcher", [])
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "backup_keep_days", 14)
    patch_pg_tool(monkeypatch, {}, archive=b"ARCHIVE")

    report = asyncio.run(backup.scheduled_backup())

    written = list((tmp_path / "backups").glob("aizk-*.dump"))
    assert len(written) == 1 and written[0].name == Path(report.path).name
    assert report.bytes == len(b"ARCHIVE")
