import socket
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from doubles import (
    RecordingEmbedder,
    RecordingReranker,
    deterministic_vector,
    install_fake_embedder,
    install_fake_reranker,
)
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

from aizk.config import Settings
from aizk.config import settings as _settings

# re-exported so existing tests keep importing the doubles and the vector helper from conftest
__all__ = [
    "RecordingEmbedder",
    "RecordingReranker",
    "deterministic_vector",
    "install_fake_embedder",
    "install_fake_reranker",
]

# the DB-backed properties open a real connection per example, so the per-example deadline is
# lifted and the suppressed health check lets a function-scoped fixture feed a property without
# tripping the guard. The example count is trimmed since each DB example is one round trip.
hypothesis_settings.register_profile(
    "aizk",
    deadline=None,
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
hypothesis_settings.register_profile(
    "aizk-db",
    deadline=None,
    max_examples=15,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
hypothesis_settings.load_profile("aizk")


def port_open(host: str | None, port: int | None, timeout: float = 0.5) -> bool:
    """Whether a TCP connection to host and port succeeds within timeout.

    host: target hostname, treated as unreachable when missing.
    port: target port, treated as unreachable when missing.
    timeout: connection deadline in seconds.
    """
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def reachable(url: str) -> bool:
    """Whether the host and port of a URL accept a TCP connection.

    url: a DSN or http URL whose authority is probed.
    """
    parts = urlsplit(url)
    return port_open(parts.hostname, parts.port)


# probed once at collection so the DB-integration tests deselect cleanly when Postgres is absent,
# the only real prerequisite an integration test gates on since every model-shaped step now lives
# in a container reached over HTTP rather than something this process loads itself.
DB_UP = reachable(_settings.database_url)


@pytest.fixture
def settings() -> Settings:
    """The shared global settings singleton, the same object every converted function reads.

    Returning the actual global rather than a fresh `Settings()` means a test that mutates it
    through `monkeypatch.setattr(settings, ...)` and one that requests this fixture see the same
    values.
    """
    return _settings


@pytest.fixture
def requires_db() -> None:
    """Skip a DB-integration test when the Postgres DSN host is unreachable."""
    if not DB_UP:
        pytest.skip("aizk postgres not reachable")


@pytest.fixture
def fake_embedder() -> Iterator[RecordingEmbedder]:
    """Install a recording embedder behind `Embedder()` for one test, text and image lanes both.

    The double is installed for the duration of the test and cleared on exit so it never leaks
    into or out of the run.
    """
    embedder = RecordingEmbedder()
    install_fake_embedder(embedder)
    yield embedder
    install_fake_embedder(None)


@pytest.fixture
def fake_reranker() -> Iterator[RecordingReranker]:
    """Install a recording reranker behind `Reranker()` for one test."""
    reranker = RecordingReranker()
    install_fake_reranker(reranker)
    yield reranker
    install_fake_reranker(None)
