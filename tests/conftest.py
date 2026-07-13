import asyncio
import socket
import uuid
from importlib import import_module
from urllib.parse import urlsplit

import a_env
import pytest
from doubles import (
    FakeLLM,
    RecordingEmbedder,
)
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from sqlalchemy import NullPool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aizk import ops
from aizk.config import Settings
from aizk.config import settings as aizk_settings

# a_env configures the isolated database before Aizk imports.
assert a_env.configured()

pytest_plugins = ["bg_doubles"]

# DB properties use fewer examples and no deadline because each draw opens a connection.
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
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_db = urlsplit(aizk_settings.database_url)
DB_UP = _port_open(_db.hostname, _db.port)


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

    asyncio.run(bootstrap())


if DB_UP:
    ensure_test_database()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: needs the live GPU model services, deselected by default"
    )


@pytest.fixture
def settings() -> Settings:
    return aizk_settings


@pytest.fixture(scope="session")
def migrated_db() -> None:
    if not DB_UP:
        pytest.skip("aizk_test postgres not reachable")


@pytest.fixture
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RecordingEmbedder:
    embedder = RecordingEmbedder()
    for name in (
        "aizk.admin",
        "aizk.eval.scale",
        "aizk.extract.ingest",
        "aizk.extract.ontology.cache",
        "aizk.graph.build",
        "aizk.graph.communities",
        "aizk.graph.insight",
        "aizk.graph.profiles",
        "aizk.graph.raptor",
        "aizk.graph.reembed",
        "aizk.retrieval.recall.orchestrator",
    ):
        module = import_module(name)
        monkeypatch.setattr(module, "embed", embedder.embed)
    monkeypatch.setattr(
        import_module("aizk.extract.ingest"), "embed_images", embedder.embed_images
    )
    return embedder


@pytest.fixture
def fake_reranker(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Score every rerank call neutrally and record the texts each call saw."""
    calls: list[list[str]] = []

    async def rerank(query: str, texts: list[str]) -> list[float]:
        del query
        calls.append(texts)
        return [0.0] * len(texts)

    monkeypatch.setattr(import_module("aizk.retrieval.rerank.rescore"), "rerank", rerank)
    return calls


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr("aizk.extract.llm.triples.client_for", lambda *args, **kwargs: fake)
    return fake


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()
