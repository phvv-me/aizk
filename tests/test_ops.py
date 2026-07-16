import io
from collections.abc import Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self

import dbutil
import httpx
import pytest
import seedgraph
from factories import CandidateFactory
from id_factory import uuid5
from pydantic import JsonValue
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import TextClause

import aizk.ops as ops
from aizk.background.status import TasksStatus
from aizk.config import settings
from aizk.ops import EndpointHealth
from aizk.retrieval import Candidate, Lane
from aizk.store import Chunk
from aizk.store.identity import User
from alembic import command


class FakeResponse:
    def __init__(self, status_code: int, payload: JsonValue = None) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> JsonValue:
        return self.payload


class HTTPClient(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool: ...

    async def get(self, url: str) -> FakeResponse: ...


def fake_async_client(
    status_code: int | None,
    error: httpx.HTTPError | None,
    payload: JsonValue = None,
) -> Callable[..., HTTPClient]:
    class Client:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool:
            del exc_type, exc, traceback
            return False

        async def get(self, url: str) -> FakeResponse:
            if error is not None:
                raise error
            assert status_code is not None
            return FakeResponse(status_code, payload)

    return Client


class FakeConnection:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.statements: list[str] = []

    async def execute(self, statement: Executable) -> None:
        self.statements.append(
            statement.text if isinstance(statement, TextClause) else type(statement).__name__
        )
        if self.error is not None:
            raise self.error


class FakeBegin:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, exc, traceback
        return False


class FakeEngine:
    def __init__(self, error: Exception | None) -> None:
        self.connection = FakeConnection(error)
        self.disposed = False

    def begin(self) -> FakeBegin:
        return FakeBegin(self.connection)

    def connect(self) -> FakeBegin:
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
    ("payload", "configured_as", "expected"),
    [
        ({"model": "gliner-large"}, None, {"model": "gliner-large"}),
        (
            {"checkpoint": "fastino/gliner2-large-v1"},
            None,
            {"model": "fastino/gliner2-large-v1"},
        ),
        (
            {"data": [{"root": "google/gemma-4-31B", "max_model_len": 3072}]},
            None,
            {"model": "google/gemma-4-31B"},
        ),
        ({"data": [{"id": "extractor"}]}, None, {"model": "extractor"}),
        (["not", "a", "model response"], None, {"model": None}),
        (
            {"data": [{"id": "extractor", "root": "google/gemma-4-31B"}]},
            "extractor",
            {"served_as": "extractor", "configured_as": "extractor", "matched": True},
        ),
        (
            {"data": [{"id": "extractor", "max_model_len": 3072}]},
            None,
            {"context_tokens": 3072},
        ),
    ],
)
def test_probe_endpoint_decodes_each_supported_model_metadata_shape(
    monkeypatch: pytest.MonkeyPatch,
    payload: JsonValue,
    configured_as: str | None,
    expected: dict[str, str | int | bool | None],
) -> None:
    monkeypatch.setattr(ops.httpx, "AsyncClient", fake_async_client(200, None, payload))

    health = dbutil.run(ops.probe_endpoint("llm", "http://x/v1", configured_as=configured_as))

    assert {field: getattr(health, field) for field in expected} == expected


def corpus() -> ops.ScopeHealth:
    """Build one deterministic health row for recall and report tests."""
    now = datetime.now(UTC)
    return ops.ScopeHealth(
        scopes=(settings.system_user_id,),
        creators=1,
        documents=1,
        chunks=1,
        processed_chunks=1,
        entities=2,
        facts=1,
        profiles=1,
        last_write_at=now,
        last_projection_at=now,
    )


def test_corpus_health_groups_storage_by_creator_and_scope(migrated_db: None) -> None:
    async def body() -> list[ops.ScopeHealth]:
        owner = uuid5()
        chunk_id = await seedgraph.seed_chunk(owner, "current project context")
        async with dbutil.admin_engine().begin() as connection:
            await connection.execute(
                update(Chunk).where(Chunk.id == chunk_id).values(processed_at=datetime.now(UTC))
            )
        return await ops.corpus_health()

    rows = dbutil.run(body())
    row = next(item for item in rows if item.documents == 1 and item.chunks == 1)

    assert row.processed_chunks == 1
    assert row.last_projection_at is not None


@pytest.mark.parametrize("failure", [False, True], ids=["candidate", "connection-error"])
def test_recall_health_reports_candidates_and_expected_failures(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    candidate = CandidateFactory.build(
        lane=Lane.Kind.SOURCES,
        line="Aizk is active and its next action is the external benchmark.",
        source_title="Aizk",
    )

    async def recalled(query: str, user: User, token_budget: int) -> list[Candidate]:
        if failure:
            raise httpx.ConnectError("refused")
        assert query
        assert user.scopes.read == frozenset(corpus().scopes)
        assert token_budget == 512
        return [candidate]

    monkeypatch.setattr(ops, "recall", recalled)

    report = dbutil.run(ops.recall_health(corpus()))

    assert report.candidates == (0 if failure else 1)
    assert report.top_source == (None if failure else "Aizk")
    assert ("Aizk is active" in report.sample) is not failure
    if failure:
        assert report.error is not None and report.error.startswith("ConnectError:")
    else:
        assert report.error is None


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


def test_reset_recreates_only_the_configured_database_then_runs_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine(None)

    async def setup() -> ops.SetupReport:
        return ops.SetupReport(migrated_from=None, migrated_to="0001_init", queue_installed=True)

    monkeypatch.setattr(ops, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(ops, "setup", setup)

    report = dbutil.run(ops.reset())

    assert report == ops.ResetReport(database=settings.db_name, migrated_to="0001_init")
    assert engine.connection.statements == [
        f'DROP DATABASE IF EXISTS "{settings.db_name}" WITH (FORCE)',
        f'CREATE DATABASE "{settings.db_name}"',
    ]
    assert engine.disposed is True


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
    expected_corpus = corpus()
    recall = ops.RecallHealth(
        query="probe",
        scopes=expected_corpus.scopes,
        candidates=1,
        top_source="Aizk",
        sample="healthy",
        latency_ms=1.0,
    )

    async def fake_probe(
        name: str, url: str, path: str, configured_as: str | None
    ) -> EndpointHealth:
        return EndpointHealth(name=name, url=url, reachable=True)

    async def fake_overview() -> TasksStatus:
        return queue

    async def fake_corpora() -> list[ops.ScopeHealth]:
        return [expected_corpus]

    async def fake_recall(selected: ops.ScopeHealth) -> ops.RecallHealth:
        assert selected == expected_corpus
        return recall

    monkeypatch.setattr(ops, "probe_endpoint", fake_probe)
    monkeypatch.setattr(ops, "tasks_overview", fake_overview)
    monkeypatch.setattr(ops, "corpus_health", fake_corpora)
    monkeypatch.setattr(ops, "recall_health", fake_recall)

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
    assert report.extraction == ops.ExtractionHealth(
        backend=settings.extract_backend,
        window_chars=settings.extract_window_size,
        output_tokens=settings.llm_extract_max_tokens,
    )
    assert report.corpora == [expected_corpus]
    assert report.recall == recall
    assert report.identity.mode == "local"
    assert report.duration_ms >= 0
    assert [endpoint.name for endpoint in report.endpoints] == [
        "embed",
        "llm",
        "rerank",
        "gliner",
    ]
    assert all(endpoint.reachable for endpoint in report.endpoints)
