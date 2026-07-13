import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import pytest

from aizk import backup
from aizk.backup import BackupError
from aizk.config import settings


class FakeProcess:
    def __init__(self, returncode: int, stderr: bytes) -> None:
        self.returncode = returncode
        self.stderr = stderr

    async def communicate(self) -> tuple[None, bytes]:
        return None, self.stderr


@dataclass
class ProcessRecord:
    args: list[str] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    stdin: BinaryIO | None = None
    stdout: BinaryIO | None = None


def patch_pg_tool(
    monkeypatch: pytest.MonkeyPatch,
    record: ProcessRecord,
    *,
    returncode: int = 0,
    reported_stderr: bytes = b"",
    archive: bytes = b"",
) -> None:
    async def fake(
        *args: str,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
        stderr: int | None = None,
    ) -> FakeProcess:
        # Scalar keys retain the first invocation while calls records every command.
        if not record.calls:
            record.args = list(args)
            record.stdin = stdin
            record.stdout = stdout
        record.calls.append(list(args))
        if stdout is not None and archive:
            stdout.write(archive)
        return FakeProcess(returncode, reported_stderr)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)


def test_backup_streams_pg_dump_to_the_path_and_reports_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "pg_client_launcher", [])
    record = ProcessRecord()
    patch_pg_tool(monkeypatch, record, archive=b"ARCHIVE")
    dump = tmp_path / "x.dump"

    report = asyncio.run(backup.backup_database(str(dump)))

    assert report.bytes == len(b"ARCHIVE")
    assert record.args[:3] == ["pg_dump", "--format=custom", "--dbname"]
    assert record.stdout is not None  # streamed to the file handle, not captured in memory


def test_restore_streams_the_archive_and_cleans_the_configured_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "pg_client_launcher", [])
    record = ProcessRecord()
    patch_pg_tool(monkeypatch, record)
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"ARCHIVE")

    report = asyncio.run(backup.restore_database(str(dump)))

    assert record.args[0] == "pg_restore"
    assert "--clean" in record.args and "--if-exists" in record.args
    assert record.stdin is not None
    assert report.database == settings.db_name
    assert len(record.calls) == 2 and record.calls[1][0] == "psql"
    assert "tokenizer_catalog.tokenize" in record.calls[1][-1]


def test_restore_recreates_the_bm25_tokenizer_when_the_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued: list[list[str]] = []

    async def fake_tool(
        args: list[str], *, stdout_path: str | None = None, stdin_path: str | None = None
    ) -> None:
        issued.append(args)
        if "tokenizer_catalog.tokenize" in args[-1]:
            raise backup.BackupError("Tokenizer not found: aizk_bm25")

    monkeypatch.setattr(settings, "pg_client_launcher", [])
    monkeypatch.setattr(backup, "run_pg_tool", fake_tool)
    asyncio.run(backup.ensure_bm25_tokenizer())
    assert any("tokenizer_catalog.create_tokenizer" in call[-1] for call in issued)

    issued.clear()

    async def healthy_tool(args: list[str], **_: object) -> None:
        issued.append(args)

    monkeypatch.setattr(backup, "run_pg_tool", healthy_tool)
    asyncio.run(backup.ensure_bm25_tokenizer())
    assert len(issued) == 1 and "tokenizer_catalog.tokenize" in issued[0][-1]


def test_restore_into_a_scratch_database_skips_the_clean_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "pg_client_launcher", [])
    record = ProcessRecord()
    patch_pg_tool(monkeypatch, record)
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"ARCHIVE")

    report = asyncio.run(backup.restore_database(str(dump), database="scratch"))

    assert "--clean" not in record.args
    assert report.database == "scratch"


def test_a_nonzero_exit_raises_backup_error_naming_the_tool_and_its_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "pg_client_launcher", [])
    patch_pg_tool(
        monkeypatch,
        ProcessRecord(),
        returncode=1,
        reported_stderr=b"aborting because of server version",
    )

    with pytest.raises(BackupError, match="pg_dump exited 1.*server version"):
        asyncio.run(backup.backup_database(str(tmp_path / "x.dump")))


def test_the_launcher_prefixes_the_command_so_the_tool_runs_in_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "pg_client_launcher", ["docker", "exec", "-i", "aizk-db-1"])
    record = ProcessRecord()
    patch_pg_tool(monkeypatch, record, archive=b"A")

    asyncio.run(backup.backup_database(str(tmp_path / "x.dump")))

    assert record.args[:5] == ["docker", "exec", "-i", "aizk-db-1", "pg_dump"]


def test_connection_url_prefers_the_override_and_swaps_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "backup_database_url", "postgresql://aizk:pw@db:5432/aizk")
    assert "db:5432/aizk" in backup.connection_url()
    assert backup.connection_url("scratch").endswith("/scratch")

    monkeypatch.setattr(settings, "backup_database_url", "")
    assert backup.connection_url() == settings.admin_asyncpg_dsn


def test_prune_backups_removes_only_the_old_matching_dumps(tmp_path: Path) -> None:
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
    monkeypatch.setattr(settings, "pg_client_launcher", [])
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "backup_keep_days", 14)
    patch_pg_tool(monkeypatch, ProcessRecord(), archive=b"ARCHIVE")

    report = asyncio.run(backup.scheduled_backup())

    written = list((tmp_path / "backups").glob("aizk-*.dump"))
    assert len(written) == 1 and written[0].name == Path(report.path).name
    assert report.bytes == len(b"ARCHIVE")
