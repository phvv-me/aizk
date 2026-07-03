import asyncio
import uuid
from collections.abc import Iterator

import pytest
from graphdb import DB_UP, FakeLLM, add_principals, drop_principals, purge_owner

from aizk.cli import migrate
from aizk.config import Settings
from aizk.config import settings as _settings
from aizk.extract.llm import triples as triples_module


@pytest.fixture(scope="session")
def migrated_db() -> None:
    """Bring the schema to head once for the whole graph DB session, or skip when Postgres is down.

    Migrating once keeps the property loops from paying a fresh migration per generated example,
    since each example only inserts and cleans a handful of rows under a fresh principal.
    """
    if not DB_UP:
        pytest.skip("aizk postgres not reachable")
    migrate()


@pytest.fixture
def fake_settings() -> Settings:
    """The global settings singleton, still requested by name for tests already keyed on it.

    Installing the recording embedder no longer needs a settings override to activate, since
    `install_fake_embedder` swaps `Embedder`'s singleton slot directly, so this fixture only hands
    back the shared global object every converted function reads.
    """
    return _settings


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """Install a recording LLM behind `client_for` for one test, the only LLM seam these mock.

    Patching `client_for` in the triples module routes every `structured` call, and so every
    extractor and summarizer, onto the recording double without touching the callers.
    """
    fake = FakeLLM()
    monkeypatch.setattr(triples_module, "client_for", lambda *_, **__: fake)
    return fake


@pytest.fixture
def fresh_principal(fake_settings: Settings, migrated_db: None) -> Iterator[uuid.UUID]:
    """A migrated database and a seeded principal under the fake-backend settings for one test.

    Yields the owning principal id, then tears the principal and its owned rows down, so a DB test
    reads as the seed-act-assert it is without repeating cleanup.
    """
    pid = uuid.uuid4()
    asyncio.run(add_principals(pid))
    try:
        yield pid
    finally:

        async def teardown() -> None:
            await purge_owner(pid)
            await drop_principals(pid)

        asyncio.run(teardown())
