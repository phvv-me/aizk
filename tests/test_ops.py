import io
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import NamedTuple, Protocol, Self
from unittest.mock import AsyncMock

import dbutil
import httpx
import pytest
import seedgraph
from factories import CandidateFactory
from id_factory import uuid5, uuid8
from patos import FrozenModel
from pydantic import JsonValue
from sqlalchemy import URL, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import TextClause
from sqlmodel import delete, select

import aizk.ops as ops
from aizk.background.status import TasksStatus
from aizk.config import DatabaseBackend, settings
from aizk.ontology import Ontology
from aizk.ops import EndpointHealth
from aizk.retrieval import Candidate, Lane
from aizk.status import UsageSummary
from aizk.store import Artifact, Blob, Chunk, OperatorReading, OperatorSnapshot, Usage
from aizk.store.backend import DatabaseRole
from aizk.store.engine import Session
from aizk.store.identity import User
from aizk.usage import UsageAccountingJob, UsageCapture
from alembic import command
from alembic.config import Config


class FakeResponse:
    def __init__(self, status_code: int, payload: JsonValue | Exception = None) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> JsonValue:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_usage_health_attributes_operations_and_deduplicated_storage() -> None:
    async def probe() -> tuple[
        list[ops.ActorUsage],
        list[ops.ScopeUsage],
        list[ops.ScopeStorage],
        ops.StorageHealth,
    ]:
        await dbutil.reset_db()
        actor, team = uuid5(), uuid5()
        events = (
            (Usage.Event.Operation.find_memory, (actor, team), 4, 10),
            (Usage.Event.Operation.keep_file, (team,), 100, 0),
            (Usage.Event.Operation.share, (team,), 0, 0),
            (Usage.Event.Operation.artifact_read, (actor,), 0, 12),
        )
        job = UsageAccountingJob()
        for index, (operation, targets, request_bytes, response_bytes) in enumerate(events):
            await job.handle(
                UsageCapture(
                    capture_key=f"span-{index}",
                    occurred_at=datetime.now(UTC),
                    user_id=actor,
                    operation=operation,
                    targets=targets,
                    request_bytes=request_bytes,
                    response_bytes=response_bytes,
                )
            )

        blob = Blob(
            content_hash=uuid8(),
            size=100,
            stored_size=60,
            encoding=Blob.Encoding.zstd,
            storage_key="objects/accounted",
        )
        artifacts = (
            Artifact(name="private.pdf", created_by=actor, scopes=[actor]),
            Artifact(name="shared.pdf", created_by=actor, scopes=[team]),
        )
        # The maintenance caller reads both target scopes so the attachment guard
        # accepts the second content revision that reuses the deduplicated blob.
        async with User.system(frozenset({actor, team})).owner as session:
            session.add(blob)
            session.add_all(artifacts)
            await session.flush()
            session.add_all(
                Artifact.Content(
                    artifact_id=artifact.id,
                    blob_id=blob.id,
                    created_by=actor,
                    scopes=artifact.scopes,
                )
                for artifact in artifacts
            )
        return await ops.usage_health()

    actors, scopes, scope_storage, storage = dbutil.run(probe())
    assert len(actors) == 1
    assert actors[0].model_dump() == {
        "actor_id": actors[0].actor_id,
        "finds": 1,
        "keeps": 1,
        "files": 1,
        "shares": 1,
        "artifact_reads": 1,
        "request_bytes": 104,
        "response_bytes": 22,
    }
    scope_usage = {item.scope_id: item for item in scopes}
    assert sorted((item.finds, item.files, item.shares) for item in scopes) == [
        (1, 0, 0),
        (1, 1, 1),
    ]
    assert sum(item.request_bytes for item in scope_usage.values()) == 108
    assert sum(item.response_bytes for item in scope_usage.values()) == 32
    assert sorted((item.artifact_revisions, item.logical_bytes) for item in scope_storage) == [
        (1, 100),
        (1, 100),
    ]
    assert storage == ops.StorageHealth(
        originals=2,
        logical_bytes=200,
        physical_blobs=1,
        original_bytes=100,
        stored_bytes=60,
        compression_saved_bytes=40,
        unverified_blobs=1,
        failed_integrity_blobs=0,
        last_integrity_check=None,
    )


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
    payload: JsonValue | Exception = None,
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

    def execution_options(self, *, isolation_level: str) -> Self:
        assert isolation_level == "AUTOCOMMIT"
        return self

    async def dispose(self) -> None:
        self.disposed = True


class FakeDatabaseAdapter:
    def __init__(self, engine: FakeEngine) -> None:
        self.fake_engine = engine

    def engine(self, url: str | URL, role: DatabaseRole) -> FakeEngine:
        del url, role
        return self.fake_engine


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
    assert "CREATE POLICY rls_select" in script


def test_alembic_config_preserves_percent_encoded_cloud_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud_url = (
        "cockroachdb+asyncpg://owner:p%25word@managed:26257/aizk"
        "?sslmode=verify-full&sslrootcert=%2Fcerts%2Froot.crt"
    )
    monkeypatch.setattr(settings, "admin_database_url", cloud_url)

    assert ops.alembic_config().get_main_option("sqlalchemy.url") == cloud_url


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
    monkeypatch.setattr(ops.probes.httpx, "AsyncClient", fake_async_client(status_code, error))

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
    monkeypatch.setattr(ops.probes.httpx, "AsyncClient", fake_async_client(200, None, payload))

    health = dbutil.run(ops.probe_endpoint("llm", "http://x/v1", configured_as=configured_as))

    assert {field: getattr(health, field) for field in expected} == expected


def test_probe_endpoint_keeps_the_row_when_the_endpoint_returns_non_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decode_error = json.JSONDecodeError("Expecting value", "<html>error</html>", 0)
    monkeypatch.setattr(
        ops.probes.httpx, "AsyncClient", fake_async_client(200, None, decode_error)
    )

    health = dbutil.run(ops.probe_endpoint("llm", "http://x/v1", configured_as="extractor"))

    assert health == EndpointHealth(
        name="llm", url="http://x/v1", reachable=True, configured_as="extractor"
    )


def corpus() -> ops.ScopeHealth:
    """Build one deterministic health row for find and report tests."""
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
def test_find_health_reports_candidates_and_expected_failures(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    candidate = CandidateFactory.build(
        lane=Lane.Kind.SOURCES,
        line="Aizk is active and its next action is the external benchmark.",
        source_title="Aizk",
    )

    async def found(query: str, user: User, k: int, token_budget: int) -> list[Candidate]:
        if failure:
            raise httpx.ConnectError("refused")
        assert query
        assert user.scopes.read == frozenset(corpus().scopes)
        assert k == 2
        assert token_budget == 512
        return [candidate]

    monkeypatch.setattr(ops.probes, "find_candidates", found)

    report = dbutil.run(ops.find_health(corpus()))

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
    monkeypatch.setattr(ops.provision, "database_adapter", lambda: FakeDatabaseAdapter(engine))

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


def test_setup_is_idempotent_on_a_ready_database(
    migrated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokenizer_checks = 0

    async def ensure_bm25_tokenizer() -> None:
        nonlocal tokenizer_checks
        tokenizer_checks += 1

    monkeypatch.setattr(ops.provision, "ensure_bm25_tokenizer", ensure_bm25_tokenizer)

    async def body() -> ops.SetupReport:
        report = await ops.setup()
        assert await ops.has_queue_schema() is True
        return report

    report = dbutil.run(body())

    assert report.queue_installed is False
    assert report.migrated_from == report.migrated_to
    assert tokenizer_checks == 1


@pytest.mark.parametrize(
    "violations",
    [[], ["artifact policy drifted"]],
    ids=["valid", "drifted"],
)
def test_cockroach_setup_uses_its_migrations_and_skips_postgres_services(
    migrated_db: None,
    monkeypatch: pytest.MonkeyPatch,
    violations: list[str],
) -> None:
    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)
    calls: list[str] = []

    async def current() -> str:
        return "0000"

    async def queue_present() -> bool:
        return False

    def migrate(fn: Callable[..., None], config: Config, revision: str) -> None:
        assert fn is command.upgrade
        assert revision == "head"
        locations = config.get_main_option("version_locations")
        assert locations is not None and "cockroachdb" in locations
        calls.append("migrate")

    async def install_queue() -> None:
        calls.append("queue")

    async def verify_row_security() -> list[str]:
        calls.append("verify")
        return violations

    async def grant() -> None:
        calls.append("grant")

    async def refresh(cls: type[Ontology], session: Session) -> None:
        del cls, session
        calls.append("ontology")

    async def postgres_only() -> None:
        raise AssertionError("PostgreSQL-only setup ran for CockroachDB")

    monkeypatch.setattr(ops.provision, "alembic_current", current)
    monkeypatch.setattr(ops.provision, "has_queue_schema", queue_present)
    monkeypatch.setattr(ops.provision, "run_alembic", migrate)
    monkeypatch.setattr(ops.provision, "row_security_violations", verify_row_security)
    monkeypatch.setattr(ops.provision, "install_queue_schema", install_queue)
    monkeypatch.setattr(ops.provision, "grant_app_role_privileges", grant)
    monkeypatch.setattr(ops.provision, "alembic_head", lambda config: "0001")
    monkeypatch.setattr(ops.provision.Ontology, "refresh", classmethod(refresh))
    monkeypatch.setattr(ops.provision, "ensure_bm25_tokenizer", postgres_only)
    monkeypatch.setattr(ops.provision, "enable_query_stats", postgres_only)

    if violations:
        with pytest.raises(RuntimeError, match="artifact policy drifted"):
            dbutil.run(ops.setup())
        assert calls == ["migrate", "verify"]
        return

    report = dbutil.run(ops.setup())

    assert report == ops.SetupReport(
        migrated_from="0000",
        migrated_to="0001",
        queue_installed=True,
    )
    assert calls == ["migrate", "verify", "queue", "grant", "ontology"]


@pytest.mark.parametrize(
    ("backend", "drop_suffix"),
    [
        (DatabaseBackend.postgresql, "WITH (FORCE)"),
        (DatabaseBackend.cockroachdb, "CASCADE"),
    ],
)
def test_reset_recreates_only_the_configured_database_then_runs_setup(
    monkeypatch: pytest.MonkeyPatch,
    backend: DatabaseBackend,
    drop_suffix: str,
) -> None:
    engine = FakeEngine(None)

    async def setup() -> ops.SetupReport:
        return ops.SetupReport(migrated_from=None, migrated_to="0001_init", queue_installed=True)

    monkeypatch.setattr(ops.provision, "database_adapter", lambda: FakeDatabaseAdapter(engine))
    monkeypatch.setattr(ops.provision, "setup", setup)
    monkeypatch.setattr(settings, "database_backend", backend)

    report = dbutil.run(ops.reset())

    assert report == ops.ResetReport(database=settings.db_name, migrated_to="0001_init")
    assert engine.connection.statements == [
        f'DROP DATABASE IF EXISTS "{settings.db_name}" {drop_suffix}',
        f'CREATE DATABASE "{settings.db_name}"',
    ]
    assert engine.disposed is True


def test_provisioning_rejects_an_unknown_database_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_backend", "sqlite")

    with pytest.raises(ValueError, match="unsupported database backend sqlite"):
        ops.provision._database_provisioning()


@pytest.mark.parametrize(
    ("admin_url", "app_url", "message"),
    [
        (
            "postgresql+asyncpg://a:p@h/postgres",
            "postgresql+asyncpg://u:p@h/postgres",
            "maintenance database",
        ),
        (
            "postgresql+asyncpg://a:p@h/template1",
            "postgresql+asyncpg://u:p@h/template1",
            "maintenance database",
        ),
        (
            "postgresql+asyncpg://a:p@h",
            "postgresql+asyncpg://u:p@h/aizk",
            "maintenance database",
        ),
        (
            "postgresql+asyncpg://a:p@h/aizk",
            "postgresql+asyncpg://u:p@h/other",
            "same database",
        ),
    ],
    ids=["postgres", "template", "unnamed", "mismatched"],
)
def test_reset_refuses_maintenance_and_mismatched_databases(
    monkeypatch: pytest.MonkeyPatch, admin_url: str, app_url: str, message: str
) -> None:
    monkeypatch.setattr(settings, "admin_database_url", admin_url)
    monkeypatch.setattr(settings, "database_url", app_url)

    with pytest.raises(ValueError, match=message):
        dbutil.run(ops.reset())


def test_setup_installs_the_queue_on_a_fresh_database(migrated_db: None) -> None:
    async def body() -> ops.SetupReport:
        await dbutil.admin_exec(
            "DROP TABLE IF EXISTS pgqueuer, pgqueuer_log, pgqueuer_statistics, "
            "pgqueuer_schedules CASCADE"
        )
        await dbutil.admin_exec("DROP TYPE IF EXISTS pgqueuer_status CASCADE")
        await dbutil.admin_exec("DROP FUNCTION IF EXISTS fn_pgqueuer_changed CASCADE")
        assert await ops.has_queue_schema() is False
        report = await ops.setup()
        assert await ops.has_queue_schema() is True
        return report

    report = dbutil.run(body())

    assert report.queue_installed is True
    assert report.migrated_from == report.migrated_to


def test_health_reads_every_section(migrated_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = TasksStatus(
        pending=3,
        running=1,
        failed=0,
        last_success=None,
        oldest_queued=None,
        projection_pending=2,
    )
    expected_corpus = corpus()
    find = ops.FindHealth(
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

    async def fake_find(selected: ops.ScopeHealth) -> ops.FindHealth:
        assert selected == expected_corpus
        return find

    monkeypatch.setattr(ops.probes, "probe_endpoint", fake_probe)
    monkeypatch.setattr(ops.probes, "tasks_overview", fake_overview)
    monkeypatch.setattr(ops.probes, "corpus_health", fake_corpora)
    monkeypatch.setattr(ops.probes, "find_health", fake_find)

    report = dbutil.run(ops.health())

    assert report.migration.up_to_date is True
    assert report.migration.current == report.migration.head
    assert report.rls_violations == []
    assert set(report.row_counts) == {
        "document",
        "artifact",
        "artifact_content",
        "blob",
        "chunk",
        "entity_content",
        "entity_claim",
        "fact_content",
        "fact_claim",
        "community",
        "profile",
        "session_item",
        "usage_event",
    }
    assert report.queue == queue
    assert report.extraction == ops.ExtractionHealth(
        backend=settings.extract_backend,
        window_chars=settings.extract_window_size,
        output_tokens=settings.llm_extract_max_tokens,
    )
    assert report.corpora == [expected_corpus]
    assert report.find == find
    assert report.identity.mode == "local"
    assert report.duration_ms >= 0
    assert [endpoint.name for endpoint in report.endpoints] == [
        "embed",
        "llm",
        "rerank",
        "gliner",
    ]
    assert all(endpoint.reachable for endpoint in report.endpoints)


def test_health_skips_the_live_find_probe_when_asked(
    migrated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected_corpus = corpus()
    called = False

    async def fake_probe(
        name: str, url: str, path: str, configured_as: str | None
    ) -> EndpointHealth:
        return EndpointHealth(name=name, url=url, reachable=True)

    async def fake_overview() -> TasksStatus:
        return TasksStatus(
            pending=0,
            running=0,
            failed=0,
            last_success=None,
            oldest_queued=None,
            projection_pending=0,
        )

    async def fake_corpora() -> list[ops.ScopeHealth]:
        return [expected_corpus]

    async def unreachable_find(selected: ops.ScopeHealth) -> ops.FindHealth:
        nonlocal called
        called = True
        raise AssertionError("the live find probe must not run")

    monkeypatch.setattr(ops.probes, "probe_endpoint", fake_probe)
    monkeypatch.setattr(ops.probes, "tasks_overview", fake_overview)
    monkeypatch.setattr(ops.probes, "corpus_health", fake_corpora)
    monkeypatch.setattr(ops.probes, "find_health", unreachable_find)

    report = dbutil.run(ops.health(include_find=False))

    assert report.find is None
    assert called is False


def _measured(row_counts: dict[str, int] | None = None) -> ops.HealthReport:
    """One complete health report, the shape a real pass produces."""
    return ops.HealthReport(
        migration=ops.SchemaHealth(current="head", head="head", up_to_date=True),
        rls_violations=[],
        row_counts=row_counts or {},
        queue=TasksStatus(
            pending=0,
            running=0,
            failed=0,
            last_success=None,
            oldest_queued=None,
            projection_pending=0,
        ),
        endpoints=[],
        extraction=ops.ExtractionHealth(backend="local", window_chars=1, output_tokens=1),
        identity=ops.IdentityHealth(mode="local", public_url=None),
        corpora=[],
        actors=[],
        scopes=[],
        scope_storage=[],
        storage=ops.StorageHealth(
            originals=0,
            logical_bytes=0,
            physical_blobs=0,
            original_bytes=0,
            stored_bytes=0,
            compression_saved_bytes=0,
            unverified_blobs=0,
            failed_integrity_blobs=0,
            last_integrity_check=None,
        ),
        find=None,
        duration_ms=1.0,
    )


def _diagnosed() -> ops.DoctorReport:
    """One complete queue and conversion diagnosis, the shape a real pass produces."""
    return ops.DoctorReport(
        generated_at=datetime(2026, 7, 1, tzinfo=UTC),
        healthy=True,
        stale_after_seconds=900,
        long_running_after_seconds=3600,
        history_seconds=86400,
        detail_limit=50,
        error_messages_included=False,
        summary=ops.DoctorSummary(),
    )


def _aggregated() -> ops.PlatformUsage:
    """One complete platform usage aggregate, the shape a real pass produces."""
    return ops.PlatformUsage(
        generated_at=datetime(2026, 7, 1, tzinfo=UTC),
        periods=(
            ops.PeriodUsage(
                days=7,
                start=datetime(2026, 6, 25, tzinfo=UTC),
                summary=UsageSummary(finds=3),
            ),
        ),
        lifetime=UsageSummary(finds=9),
        points=(),
        by_actor=(),
        by_scope=(),
    )


def operator() -> User:
    """One caller holding the Logto operator role, the only standing these readings answer."""
    return User.authorized(uuid5(), roles=(settings.logto_admin_role,))


class SnapshotCase(NamedTuple):
    """One operator reading under test, with how to fake it and how to read it back."""

    job: type[ops.SnapshotJob]
    probe: str
    measure: Callable[[], FrozenModel]
    read: Callable[[Session], Awaitable[ops.Measured | None]]
    stale_minutes: int


READINGS = (
    pytest.param(
        SnapshotCase(
            ops.HealthSnapshotJob,
            "health",
            _measured,
            ops.stored_health,
            settings.health_snapshot_stale_minutes,
        ),
        id="health",
    ),
    pytest.param(
        SnapshotCase(
            ops.DoctorSnapshotJob,
            "doctor",
            _diagnosed,
            ops.stored_doctor,
            settings.doctor_snapshot_stale_minutes,
        ),
        id="doctor",
    ),
    pytest.param(
        SnapshotCase(
            ops.UsageSnapshotJob,
            "platform_usage",
            _aggregated,
            ops.stored_usage,
            settings.usage_snapshot_stale_minutes,
        ),
        id="usage",
    ),
)


@pytest.mark.parametrize("case", READINGS)
def test_each_reading_is_measured_by_the_worker_and_read_back_by_the_app_role(
    case: SnapshotCase, migrated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker holds the owner credential the console's process is denied, so it measures.

    Reading back through the application role is the property that matters: that is the role
    the API runs as, and asking it to take the measurement itself is what failed before.
    """
    measured = case.measure()
    monkeypatch.setattr(ops.snapshot, case.probe, AsyncMock(return_value=measured))

    async def run() -> ops.Measured | None:
        await case.job().execute()
        async with operator() as session:
            return await case.read(session)

    stored = dbutil.run(run())

    assert stored is not None
    assert stored.model_dump(exclude={"measured_at", "stale"}) == measured.model_dump()
    assert stored.stale is False


@pytest.mark.parametrize("case", READINGS)
def test_each_reading_is_absent_until_a_pass_has_run(
    case: SnapshotCase, migrated_db: None
) -> None:
    async def read() -> ops.Measured | None:
        async with User.system().owner as session:
            await session.exec(delete(OperatorSnapshot))
        async with operator() as session:
            return await case.read(session)

    assert dbutil.run(read()) is None


@pytest.mark.parametrize("case", READINGS)
def test_each_reading_older_than_its_own_window_is_marked_stale(
    case: SnapshotCase, migrated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops.snapshot, case.probe, AsyncMock(return_value=case.measure()))

    async def run() -> ops.Measured | None:
        await case.job().execute()
        stale_at = datetime.now(UTC) - timedelta(minutes=case.stale_minutes + 1)
        async with User.system().owner as session:
            await session.exec(
                update(OperatorSnapshot)
                .where(OperatorSnapshot.key == case.job.reading)
                .values(updated_at=stale_at)
            )
        async with operator() as session:
            return await case.read(session)

    stored = dbutil.run(run())

    assert stored is not None and stored.stale is True


def test_a_second_pass_replaces_the_reading_rather_than_accumulating(
    migrated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = [
        _measured({"pass": 1}),
        _measured({"pass": 2}),
    ]
    monkeypatch.setattr(ops.snapshot, "health", AsyncMock(side_effect=reports))

    async def run() -> tuple[int, dict[str, int]]:
        await ops.HealthSnapshotJob().execute()
        await ops.HealthSnapshotJob().execute()
        async with User.system().owner as session:
            rows = len(
                (
                    await session.exec(
                        select(OperatorSnapshot).where(
                            OperatorSnapshot.key == OperatorReading.health
                        )
                    )
                ).all()
            )
        async with operator() as session:
            latest = await ops.stored_health(session)
        assert latest is not None
        return rows, latest.row_counts

    rows, counts = dbutil.run(run())

    assert rows == 1
    assert counts == {"pass": 2}


def test_readings_are_kept_apart_rather_than_overwriting_each_other(
    migrated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every reading shares one table, so each must land under its own key."""
    monkeypatch.setattr(ops.snapshot, "health", AsyncMock(return_value=_measured({"pass": 1})))
    monkeypatch.setattr(ops.snapshot, "doctor", AsyncMock(return_value=_diagnosed()))
    monkeypatch.setattr(ops.snapshot, "platform_usage", AsyncMock(return_value=_aggregated()))

    async def run() -> tuple[
        ops.StoredHealth | None,
        ops.StoredDoctor | None,
        ops.StoredUsage | None,
    ]:
        for job in (ops.HealthSnapshotJob(), ops.DoctorSnapshotJob(), ops.UsageSnapshotJob()):
            await job.execute()
        async with operator() as session:
            return (
                await ops.stored_health(session),
                await ops.stored_doctor(session),
                await ops.stored_usage(session),
            )

    health, doctor, usage = dbutil.run(run())

    assert health is not None and health.row_counts == {"pass": 1}
    assert doctor is not None and doctor.detail_limit == 50
    assert usage is not None and usage.lifetime.finds == 9


@pytest.mark.parametrize("case", READINGS)
def test_a_caller_without_the_operator_role_is_refused_by_row_security(
    case: SnapshotCase, migrated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console's own check is not the only thing keeping these readings from a tenant.

    They carry platform-wide material the rest of the schema protects with row security,
    including the names of artifacts whose own table is scoped, so an endpoint that forgot
    to check the role would still return nothing.
    """
    monkeypatch.setattr(ops.snapshot, case.probe, AsyncMock(return_value=case.measure()))

    async def run() -> tuple[ops.Measured | None, ops.Measured | None]:
        await case.job().execute()
        async with User.private(uuid5()) as session:
            tenant = await case.read(session)
        async with operator() as session:
            return tenant, await case.read(session)

    tenant, seen = dbutil.run(run())

    assert tenant is None
    assert seen is not None
