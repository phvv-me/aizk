import abc
from functools import partial
from typing import TYPE_CHECKING, ClassVar, cast

from pgqueuer import PgQueuer
from pgqueuer.executors import DatabaseRetryEntrypointExecutor
from pgqueuer.models import Job

from .models import QueuePayload

if TYPE_CHECKING:
    from .client import Queue


class QueueJob[PayloadT: QueuePayload](abc.ABC):
    """Declarative PgQueuer job with typed payload and uniform recovery policy."""

    entrypoint: ClassVar[str]
    payload_type: ClassVar[type[QueuePayload]]
    priority: ClassVar[int] = 0
    concurrency_limit: ClassVar[int] = 0
    max_attempts: ClassVar[int] = 5

    @abc.abstractmethod
    async def handle(self, payload: PayloadT) -> None:
        """Execute one validated payload."""

    async def consume(self, job: Job) -> None:
        """Decode one raw PgQueuer job and execute its typed body."""
        assert job.payload is not None
        await self.handle(cast(PayloadT, self.payload_type.decode(job.payload)))

    def bind(self, worker: PgQueuer) -> None:
        """Register this job with retries and retained terminal failures."""
        worker.entrypoint(
            self.entrypoint,
            concurrency_limit=self.concurrency_limit,
            on_failure="hold",
            executor_factory=partial(
                DatabaseRetryEntrypointExecutor,
                max_attempts=self.max_attempts,
            ),
        )(self.consume)

    async def enqueue(
        self,
        queue: Queue,
        payload: PayloadT,
        dedupe_key: str,
    ) -> bool:
        """Enqueue this typed job once for its deduplication key."""
        return await queue.enqueue(self, payload, dedupe_key)
