import asyncio
from collections.abc import Mapping
from functools import cache

from loguru import logger
from mangum.types import LambdaContext

from .. import ops
from ..background.queue import Queue
from ..background.schedule import run_worker_once
from ..config import settings
from ..runtime import Runtime
from ..store.mixins.base import Json
from .aws_observability import instrument


@cache
def worker_loop() -> asyncio.AbstractEventLoop:
    """Keep Lambda's database pool and async clients on one warm execution loop."""
    return asyncio.new_event_loop()


async def drain() -> dict[str, Json]:
    """Drain one portable queue wave and return its durable state for monitoring."""
    async with Runtime.assemble(settings) as runtime:
        instrument(runtime.database)
        handled = await run_worker_once(runtime)
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            snapshot = await queue.snapshot()
    return {
        "handled": handled,
        "pending": snapshot.pending,
        "running": snapshot.running,
        "failed": snapshot.failed,
        "last_success_at": snapshot.last_success.isoformat() if snapshot.last_success else None,
        "oldest_queued_at": snapshot.oldest_queued.isoformat() if snapshot.oldest_queued else None,
    }


def worker_handler(
    event: Mapping[str, Json],
    context: LambdaContext,
) -> dict[str, Json]:
    """Dispatch an explicit operator setup or durable worker invocation."""
    match event.get("kind"):
        case "worker":
            report = worker_loop().run_until_complete(drain())
            logger.bind(request_id=context.aws_request_id, **report).info(
                "portable queue drain complete"
            )
            if report["failed"] != 0:
                raise RuntimeError(f"portable queue retains {report['failed']} terminal failures")
            return report
        case "setup":
            report = ops.SetupReport.model_validate(
                worker_loop().run_until_complete(ops.setup())
            ).model_dump(mode="json")
            logger.bind(request_id=context.aws_request_id, **report).info(
                "database setup complete"
            )
            return report
        case kind:
            raise ValueError(f"unsupported worker event kind {kind!r}")
