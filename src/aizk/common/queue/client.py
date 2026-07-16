from functools import cached_property
from types import TracebackType
from typing import Self

import asyncpg
from patos import FrozenModel
from pgqueuer import PgQueuer, Queries
from pgqueuer.db import AsyncpgDriver
from pgqueuer.errors import DuplicateJobError
from pydantic import PrivateAttr

from .job import QueueJob
from .models import QueuePayload


class Queue(FrozenModel):
    """Typed application boundary over one PgQueuer connection."""

    dsn: str
    _connection: asyncpg.Connection | None = PrivateAttr(default=None)

    async def __aenter__(self) -> Self:
        """Open the queue connection."""
        assert self.__pydantic_private__ is not None
        self.__pydantic_private__["_connection"] = await asyncpg.connect(self.dsn)
        self.__dict__.pop("queries", None)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the queue connection."""
        connection = self.connection
        assert self.__pydantic_private__ is not None
        self.__pydantic_private__["_connection"] = None
        self.__dict__.pop("queries", None)
        await connection.close()

    @property
    def connection(self) -> asyncpg.Connection:
        """Return the active queue connection."""
        if self._connection is None:
            raise RuntimeError("queue is not open")
        return self._connection

    @cached_property
    def queries(self) -> Queries:
        """Build the query facade when the first queue operation needs it."""
        return Queries(AsyncpgDriver(self.connection))

    def worker(self) -> PgQueuer:
        """Build a PgQueuer worker over this queue connection."""
        return PgQueuer.from_asyncpg_connection(self.connection)

    async def enqueue[PayloadT: QueuePayload](
        self,
        job: QueueJob[PayloadT],
        payload: PayloadT,
        dedupe_key: str,
    ) -> bool:
        """Persist one typed job and report whether deduplication admitted it."""
        try:
            await self.queries.enqueue(
                job.entrypoint,
                payload.encode(),
                priority=job.priority,
                dedupe_key=dedupe_key,
            )
        except DuplicateJobError:
            return False
        return True

    async def requeue_failed[PayloadT: QueuePayload](
        self, job: QueueJob[PayloadT], limit: int = 100
    ) -> int:
        """Requeue retained failures for one typed job through PgQueuer's own API."""
        failed = await self.queries.list_failed_jobs(limit=limit)
        ids = [row.id for row in failed if row.entrypoint == job.entrypoint]
        if ids:
            await self.queries.requeue_jobs(ids)
        return len(ids)
