import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import aizk.background.status as status_mod
from aizk.background.status import tasks_overview


def patch_counts(monkeypatch: pytest.MonkeyPatch, values: list[object]) -> None:
    """Swap the status seam for a connection that returns canned queue counts in query order.

    monkeypatch: the pytest patcher.
    values: the fetchval results in the order status reads pending, running, failed, last_run, lag.
    """
    pending = iter(values)

    async def fetchval(query: str, *args: object) -> object:
        return next(pending)

    async def close() -> None:
        return None

    async def connect(dsn: str) -> SimpleNamespace:
        return SimpleNamespace(fetchval=fetchval, close=close)

    monkeypatch.setattr(status_mod, "asyncpg", SimpleNamespace(connect=connect))


def test_tasks_overview_renders_the_live_queue_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The overview reports the queue counts and the last run as an ISO timestamp."""
    last = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    patch_counts(monkeypatch, [3, 1, 2, last, 4])

    status = asyncio.run(tasks_overview())

    assert (status.pending, status.running, status.failed, status.lag) == (3, 1, 2, 4)
    assert status.last_run == last.isoformat()


def test_tasks_overview_defaults_an_empty_queue_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A queue that has never run reports zeros and a null last-run rather than None counts."""
    patch_counts(monkeypatch, [None, None, None, None, None])

    status = asyncio.run(tasks_overview())

    assert (status.pending, status.running, status.failed, status.lag) == (0, 0, 0, 0)
    assert status.last_run is None
