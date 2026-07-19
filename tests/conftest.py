import asyncio
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock
from urllib.parse import urlsplit

import a_env
import dbutil
import pytest
from doubles import (
    FakeLLM,
    NeutralGate,
    NeutralReranker,
    RecordingEmbedder,
)
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from id_factory import uuid5
from pydantic import UUID5
from sqlalchemy import NullPool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aizk import ops
from aizk.config import Settings
from aizk.config import settings as aizk_settings
from aizk.runtime import Runtime
from aizk.serving.embed import EmbedClient
from aizk.serving.gate import GateClient
from aizk.serving.rerank import RerankClient

# a_env configures the isolated database before Aizk imports.
assert a_env.configured()

pytest_plugins = ["bg_doubles"]

# Pure properties keep a generous per-example deadline. Database properties lower their example
# count locally when every draw opens a transaction.
hypothesis_settings.register_profile(
    "aizk",
    deadline=2000,
    max_examples=60,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
hypothesis_settings.load_profile("aizk")


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register benchmark reporting and regression-gate modes."""
    group = parser.getgroup("aizk evaluation")
    group.addoption(
        "--eval-mode",
        choices=("report", "gate"),
        default=None,
        help="run retrieval benchmarks in report or regression-gate mode",
    )


@pytest.fixture(scope="session")
def eval_mode(pytestconfig: pytest.Config) -> str:
    """Return the explicitly selected benchmark mode."""
    mode = pytestconfig.getoption("--eval-mode")
    if mode is None:
        raise pytest.UsageError("benchmark tests require --eval-mode=report or --eval-mode=gate")
    return str(mode)


def _port_open(host: str | None, port: int | None, timeout: float = 0.5) -> bool:
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_db = urlsplit(aizk_settings.database_url)
DB_UP = _port_open(_db.hostname, _db.port)


@contextmanager
def stubbed_model_lanes() -> Iterator[None]:
    """Point every model lane at an in-process double for the duration of the block.

    The suite is hermetic above the database seam, so ontology bootstrap, recall, extraction,
    and every other path resolve their embedder, reranker, gate, and extraction model to a
    recording double instead of reaching a live service.
    """
    extraction_model = FakeLLM().model
    with (
        mock.patch.object(
            EmbedClient, "from_settings", classmethod(lambda cls, config: RecordingEmbedder())
        ),
        mock.patch.object(
            RerankClient, "from_settings", classmethod(lambda cls, config: NeutralReranker())
        ),
        mock.patch.object(
            GateClient,
            "from_settings",
            classmethod(lambda cls, config, variant="": NeutralGate()),
        ),
        mock.patch("aizk.serving.extract.client.llm_model", lambda *args: extraction_model),
    ):
        yield


def ensure_test_database() -> None:
    async def bootstrap() -> None:
        maintenance = make_url(aizk_settings.admin_database_url).set(database="postgres")
        engine = create_async_engine(maintenance, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        name = aizk_settings.db_name
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                await connection.execute(text(f'CREATE DATABASE "{name}"'))
        finally:
            await engine.dispose()
        await ops.setup()

    # Ontology bootstrap embeds every entity description, so it must resolve to a double before
    # `ops.setup` runs, well before any function-scoped fixture could patch the seam.
    with stubbed_model_lanes():
        asyncio.run(bootstrap())


def drop_test_database() -> None:
    async def drop() -> None:
        maintenance = make_url(aizk_settings.admin_database_url).set(database="postgres")
        engine = create_async_engine(maintenance, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{aizk_settings.db_name}" WITH (FORCE)')
                )
        finally:
            await engine.dispose()

    asyncio.run(drop())


@pytest.fixture(scope="session", autouse=True)
def isolated_database(pytestconfig: pytest.Config) -> Iterator[None]:
    """Create a test database unless a live-corpus benchmark was requested."""
    if pytestconfig.getoption("--eval-mode") is not None:
        yield
        return
    if not DB_UP:
        yield
        return
    ensure_test_database()
    try:
        yield
    finally:
        dbutil.close_runner()
        drop_test_database()


@pytest.fixture
def settings() -> Settings:
    return aizk_settings


@pytest.fixture(scope="session")
def runtime() -> Runtime:
    """The one per-process service graph, assembled exactly like a serving entrypoint."""
    return Runtime.assemble(aizk_settings)


@pytest.fixture(scope="session")
def migrated_db() -> None:
    if not DB_UP:
        pytest.skip("aizk_test postgres not reachable")


@pytest.fixture(autouse=True)
def stub_model_lanes(request: pytest.FixtureRequest) -> Iterator[None]:
    """Default every model lane to an in-process double so no test reaches a live service.

    A test that must exercise real client construction opts out with the `real_services`
    marker, and the live-corpus and integration suites opt out through their marker or
    `AIZK_INTEGRATION_REAL_SERVICES`. Tests that assert on recorded calls request the explicit
    `fake_*` fixture, whose monkeypatch is installed after this default and so wins.
    """
    opts_out = (
        os.environ.get("AIZK_INTEGRATION_REAL_SERVICES") == "1"
        or request.config.getoption("--eval-mode") is not None
        or any(
            request.node.get_closest_marker(marker)
            for marker in ("real_services", "integration", "benchmark")
        )
    )
    if opts_out:
        yield
        return
    with stubbed_model_lanes():
        yield


@pytest.fixture
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RecordingEmbedder:
    embedder = RecordingEmbedder()
    monkeypatch.setattr(EmbedClient, "from_settings", classmethod(lambda cls, config: embedder))
    return embedder


@pytest.fixture
def fake_reranker(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Score every rerank call neutrally and record the texts each call saw."""
    reranker = NeutralReranker()
    monkeypatch.setattr(RerankClient, "from_settings", classmethod(lambda cls, config: reranker))
    return reranker.calls


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr("aizk.serving.extract.client.llm_model", lambda *args: fake.model)
    return fake


@pytest.fixture
def user_id() -> UUID5:
    return uuid5()
