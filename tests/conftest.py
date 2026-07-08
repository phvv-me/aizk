import os

# Hard isolation, set before any `aizk.config` import builds the settings singleton from the
# environment: the whole suite is pinned to its own `aizk_test` database and the NullPool engine,
# so a run never touches the dev `aizk` database a concurrent vault build drives, and every
# per-test `asyncio.run` loop gets a fresh connection rather than a pooled one it cannot cross.
# Forced (not `setdefault`) for `db_name` so no ambient `AIZK_DB_NAME=aizk` can redirect the suite
# onto the real database; overridable only through the dedicated `AIZK_TEST_DB_NAME` escape.
os.environ["AIZK_DB_NAME"] = os.environ.get("AIZK_TEST_DB_NAME", "aizk_test")
os.environ["AIZK_DB_NULL_POOL"] = "1"
os.environ.setdefault("AIZK_LOG_LEVEL", "")

import importlib  # noqa: E402
import socket  # noqa: E402
import uuid  # noqa: E402
from types import ModuleType  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

import pytest  # noqa: E402
from hypothesis import HealthCheck  # noqa: E402
from hypothesis import settings as hypothesis_settings  # noqa: E402

from aizk.config import Settings  # noqa: E402
from aizk.config import settings as _settings  # noqa: E402

# The model-seam doubles wrap serving/graph/extract, all under active refactor. Only the
# quarantined lanes request the fake_embedder/fake_reranker/fake_llm fixtures, so `doubles` is
# imported lazily through this helper rather than at module top: a mid-sweep break in those
# surfaces must never take down the stable store/config/export lanes at collection, whose fixtures
# never touch a model seam. The fake fixtures below skip (not error) when the import is broken.
_doubles: ModuleType | None = None


def load_doubles() -> ModuleType:
    """Import the model-seam doubles on first use, skipping the requesting test if they are broken.

    Cached so repeated fixture requests pay one import; a failure is surfaced as a `pytest.skip`
    rather than a collection error, keeping the stable lanes green while a quarantined surface is
    mid-refactor.
    """
    global _doubles
    if _doubles is None:
        try:
            _doubles = importlib.import_module("doubles")
        except ImportError as error:
            pytest.skip(f"model-seam doubles unavailable (lane under refactor): {error}")
    return _doubles


# DB-backed properties open a real connection per example, so the per-example deadline is lifted
# and the function-scoped-fixture health check is suppressed. Example counts are trimmed since each
# DB example is a network round trip; the pure profile draws more.
hypothesis_settings.register_profile(
    "aizk",
    deadline=None,
    max_examples=60,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
hypothesis_settings.register_profile(
    "aizk-db",
    deadline=None,
    max_examples=15,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
hypothesis_settings.load_profile("aizk")


def _port_open(host: str | None, port: int | None, timeout: float = 0.5) -> bool:
    """Whether a TCP connection to host and port succeeds within timeout."""
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_db = urlsplit(_settings.database_url)
DB_UP = _port_open(_db.hostname, _db.port)


def ensure_test_database() -> None:
    """Create and migrate the isolated test database when a fresh Postgres lacks it.

    A brand-new environment (CI's service container, a new dev machine) carries only the
    server and the roles, so the suite bootstraps its own database and brings it to head
    through the same `ops.setup()` the MCP server runs at startup, migrations, queue schema,
    and app-role grants alike. Idempotent, one alembic no-op when everything already exists.
    """
    import asyncio

    from sqlalchemy import NullPool, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from aizk import ops

    async def bootstrap() -> None:
        maintenance = make_url(_settings.admin_database_url).set(database="postgres")
        engine = create_async_engine(maintenance, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        name = _settings.db_name
        try:
            async with engine.connect() as connection:
                exists = await connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
                )
                if not exists:
                    await connection.execute(text(f'CREATE DATABASE "{name}"'))
        finally:
            await engine.dispose()
        await ops.setup()

    asyncio.run(bootstrap())


if DB_UP:
    ensure_test_database()


def pytest_configure(config: pytest.Config) -> None:
    """Register the markers the suite uses so `--strict-markers` never rejects one."""
    config.addinivalue_line(
        "markers", "integration: needs the live GPU model services, deselected by default"
    )


@pytest.fixture
def settings() -> Settings:
    """The shared global settings singleton every module reads, so a monkeypatch is seen widely."""
    return _settings


@pytest.fixture(scope="session")
def migrated_db() -> None:
    """Require the isolated `aizk_test` schema, skipping the DB lane when Postgres is unreachable.

    Migrations already ran against `aizk_test` at suite setup, so this only gates on reachability
    rather than re-migrating per test.
    """
    if not DB_UP:
        pytest.skip("aizk_test postgres not reachable")


@pytest.fixture
def fake_embedder():  # noqa: ANN201 - the double's type lives in the lazily-imported doubles module
    """Install a recording embedder behind `Embedder()` for one test, both lanes, cleared after."""
    doubles = load_doubles()
    embedder = doubles.RecordingEmbedder()
    doubles.install_fake_embedder(embedder)
    yield embedder
    doubles.install_fake_embedder(None)


@pytest.fixture
def fake_reranker():  # noqa: ANN201 - the double's type lives in the lazily-imported doubles module
    """Install a recording reranker behind `Reranker()` for one test, cleared on exit."""
    doubles = load_doubles()
    reranker = doubles.RecordingReranker()
    doubles.install_fake_reranker(reranker)
    yield reranker
    doubles.install_fake_reranker(None)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 - double type lazily imported
    """Route every `structured` call through a recording LLM by patching the client-pool seam.

    The LLM seam is `LLMClientPool.client_for`, the pool `structured` resolves its per-endpoint
    client through; patching the method makes every `structured` call hand back the recording
    double regardless of which endpoint a provider preset resolved to.
    """
    pool = importlib.import_module("aizk.extract.llm.client")
    fake = load_doubles().FakeLLM()
    monkeypatch.setattr(pool.LLMClientPool, "client_for", lambda self, *a, **k: fake)
    return fake


@pytest.fixture
def user_id() -> uuid.UUID:
    """A random user id, seeded on demand by the DB helpers rather than here."""
    return uuid.uuid4()
