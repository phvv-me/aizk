import io
import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self

import dbutil
import httpx
import pytest
import seedgraph
from factories import CandidateFactory
from id_factory import uuid5, uuid8
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
from aizk.store import Artifact, Blob, Chunk, Usage
from aizk.store.identity import User
from aizk.usage import UsageAccountingJob, UsageCapture
from alembic import command


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
            (Usage.Event.Operation.recall, (actor, team), 4, 10),
            (Usage.Event.Operation.remember_file, (team,), 100, 0),
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
        "recalls": 1,
        "remembers": 1,
        "files": 1,
        "shares": 1,
        "artifact_reads": 1,
        "request_bytes": 104,
        "response_bytes": 22,
    }
    scope_usage = {item.scope_id: item for item in scopes}
    assert sorted((item.recalls, item.files, item.shares) for item in scopes) == [
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

    async def recalled(query: str, user: User, k: int, token_budget: int) -> list[Candidate]:
        if failure:
            raise httpx.ConnectError("refused")
        assert query
        assert user.scopes.read == frozenset(corpus().scopes)
        assert k == 2
        assert token_budget == 512
        return [candidate]

    monkeypatch.setattr(ops.probes, "recall", recalled)

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
    monkeypatch.setattr(ops.provision, "create_async_engine", lambda url: engine)

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
        assert await ops.queue_schema_present() is True
        return report

    report = dbutil.run(body())

    assert report.queue_installed is False
    assert report.migrated_from == report.migrated_to
    assert tokenizer_checks == 1


def test_reset_recreates_only_the_configured_database_then_runs_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine(None)

    async def setup() -> ops.SetupReport:
        return ops.SetupReport(migrated_from=None, migrated_to="0001_init", queue_installed=True)

    monkeypatch.setattr(ops.provision, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(ops.provision, "setup", setup)

    report = dbutil.run(ops.reset())

    assert report == ops.ResetReport(database=settings.db_name, migrated_to="0001_init")
    assert engine.connection.statements == [
        f'DROP DATABASE IF EXISTS "{settings.db_name}" WITH (FORCE)',
        f'CREATE DATABASE "{settings.db_name}"',
    ]
    assert engine.disposed is True


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
        assert await ops.queue_schema_present() is False
        report = await ops.setup()
        assert await ops.queue_schema_present() is True
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

    monkeypatch.setattr(ops.probes, "probe_endpoint", fake_probe)
    monkeypatch.setattr(ops.probes, "tasks_overview", fake_overview)
    monkeypatch.setattr(ops.probes, "corpus_health", fake_corpora)
    monkeypatch.setattr(ops.probes, "recall_health", fake_recall)

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
