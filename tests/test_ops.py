import io
from collections.abc import Callable

import dbutil
import httpx
import pytest
from sqlalchemy.exc import DBAPIError

import aizk.ops as ops
from aizk.background.status import TasksStatus
from aizk.config import settings
from aizk.ops import EndpointHealth
from alembic import command


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def fake_async_client(
    status_code: int | None, error: httpx.HTTPError | None
) -> Callable[..., object]:
    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def get(self, url: str) -> FakeResponse:
            if error is not None:
                raise error
            assert status_code is not None
            return FakeResponse(status_code)

    return Client


class FakeConnection:
    def __init__(self, error: Exception | None) -> None:
        self.error = error

    async def execute(self, statement: object) -> None:
        if self.error is not None:
            raise self.error


class FakeBegin:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeEngine:
    def __init__(self, error: Exception | None) -> None:
        self.connection = FakeConnection(error)
        self.disposed = False

    def begin(self) -> FakeBegin:
        return FakeBegin(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


def dbapi_error(message: str) -> DBAPIError:
    return DBAPIError("CREATE EXTENSION", {}, Exception(message))


def test_alembic_config_and_head_read_the_packaged_migrations() -> None:
    config = ops.alembic_config()

    location = config.get_main_option("script_location")
    assert location is not None and location.endswith("migrations")
    assert config.get_main_option("sqlalchemy.url") == settings.admin_database_url
    head = ops.alembic_head(config)
    assert isinstance(head, str) and head
    output = io.StringIO()
    config.output_buffer = output
    ops.run_alembic(command.upgrade, config, "head", sql=True)
    script = output.getvalue()
    assert "CREATE TABLE document" in script
    assert "FORCE ROW LEVEL SECURITY" in script
    assert "CREATE POLICY scope_read" in script


def test_run_alembic_forwards_args_and_returns_off_thread() -> None:
    result = ops.run_alembic(lambda config, revision: (config, revision), "cfg", "head")

    assert result == ("cfg", "head")


def test_alembic_revision_changes_are_committed(migrated_db: None) -> None:
    config = ops.alembic_config()
    head = ops.alembic_head(config)

    try:
        ops.run_alembic(command.stamp, config, "base")
        assert dbutil.run(ops.alembic_current()) is None
    finally:
        ops.run_alembic(command.stamp, config, "head")

    assert dbutil.run(ops.alembic_current()) == head


@pytest.mark.parametrize(
    ("status_code", "error", "reachable"),
    [
        (200, None, True),
        (503, None, False),
        (None, httpx.ConnectError("refused"), False),
    ],
    ids=["ok", "server-error", "network-error"],
)
def test_probe_endpoint_maps_status_and_errors_to_reachability(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int | None,
    error: httpx.HTTPError | None,
    reachable: bool,
) -> None:
    monkeypatch.setattr(ops.httpx, "AsyncClient", fake_async_client(status_code, error))

    health = dbutil.run(ops.probe_endpoint("embed", "http://x/v1"))

    assert health == EndpointHealth(name="embed", url="http://x/v1", reachable=reachable)


@pytest.mark.parametrize(
    ("error", "raises"),
    [
        (None, False),
        (dbapi_error("pg_stat_statements must be loaded via shared_preload_libraries"), False),
        (dbapi_error("relation does not exist"), True),
    ],
    ids=["created", "not-preloaded-tolerated", "other-error-reraised"],
)
def test_enable_query_stats_tolerates_only_the_preload_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception | None, raises: bool
) -> None:
    engine = FakeEngine(error)
    monkeypatch.setattr(ops, "create_async_engine", lambda url: engine)

    if raises:
        with pytest.raises(DBAPIError):
            dbutil.run(ops.enable_query_stats())
    else:
        dbutil.run(ops.enable_query_stats())
    assert engine.disposed is True


def test_grant_app_role_privileges_is_idempotent(migrated_db: None) -> None:
    async def body() -> None:
        await ops.grant_app_role_privileges()
        await ops.grant_app_role_privileges()

    dbutil.run(body())


def test_setup_is_idempotent_on_a_ready_database(migrated_db: None) -> None:
    async def body() -> ops.SetupReport:
        report = await ops.setup()
        assert await ops.queue_schema_present() is True
        return report

    report = dbutil.run(body())

    assert report.queue_installed is False
    assert report.migrated_from == report.migrated_to


def test_setup_installs_the_queue_on_a_fresh_database(migrated_db: None) -> None:
    async def body() -> ops.SetupReport:
        await dbutil.admin_exec(
            "DROP TABLE IF EXISTS pgqueuer, pgqueuer_log, pgqueuer_statistics, "
            "pgqueuer_schedules CASCADE"
        )
        await dbutil.admin_exec("DROP TYPE IF EXISTS pgqueuer_status CASCADE")
        await dbutil.admin_exec("DROP FUNCTION IF EXISTS fn_pgqueuer_changed CASCADE")
        assert await ops.queue_schema_present() is False
        report = await ops.setup()
        assert await ops.queue_schema_present() is True
        return report

    report = dbutil.run(body())

    assert report.queue_installed is True
    assert report.migrated_from == report.migrated_to


def test_health_reads_every_section(migrated_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = TasksStatus(pending=3, running=1, failed=0, last_run=None, lag=2)

    async def fake_probe(name: str, url: str) -> EndpointHealth:
        return EndpointHealth(name=name, url=url, reachable=True)

    async def fake_overview() -> TasksStatus:
        return queue

    monkeypatch.setattr(ops, "probe_endpoint", fake_probe)
    monkeypatch.setattr(ops, "tasks_overview", fake_overview)

    report = dbutil.run(ops.health())

    assert report.migration.up_to_date is True
    assert report.migration.current == report.migration.head
    assert report.rls_violations == []
    assert set(report.row_counts) == {
        "document",
        "chunk",
        "entity_content",
        "entity_claim",
        "fact_content",
        "fact_claim",
        "community",
        "profile",
        "session_item",
    }
    assert report.queue == queue
    assert [endpoint.name for endpoint in report.endpoints] == ["embed", "llm"]
    assert all(endpoint.reachable for endpoint in report.endpoints)
