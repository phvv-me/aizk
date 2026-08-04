import asyncio
from collections.abc import Mapping
from functools import cache

from loguru import logger
from mangum import Mangum
from mangum.types import LambdaContext

from .. import ops
from ..background.queue import Queue
from ..background.schedule import run_worker_once
from ..background.wake import configured_worker_wake
from ..config import settings
from ..mcp.server import AizkMCP
from ..runtime import Runtime
from ..store.engine import Database
from ..store.mixins.base import Json
from ..usage import observe


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


@cache
def instrument(database: Database) -> None:
    """Install process tracing once while Lambda reuses a warm execution environment."""
    observe(database)


def worker_handler(
    event: Mapping[str, Json],
    context: LambdaContext,
) -> dict[str, Json]:
    """Handle an EventBridge Scheduler invocation for the portable worker."""
    report = asyncio.run(drain())
    logger.bind(request_id=context.aws_request_id, **report).info("portable queue drain complete")
    if report["failed"] != 0:
        raise RuntimeError(f"portable queue retains {report['failed']} terminal failures")
    return report


def setup_handler(
    event: Mapping[str, Json],
    context: LambdaContext,
) -> dict[str, Json]:
    """Apply database migrations from an explicitly invoked deployment function."""
    report = ops.SetupReport.model_validate(asyncio.run(ops.setup())).model_dump(mode="json")
    logger.bind(request_id=context.aws_request_id, **report).info("database setup complete")
    return report


@cache
def mcp_application() -> Mangum:
    """Build the long-lived MCP application only in the public Lambda process."""
    runtime = Runtime.assemble(settings)
    instrument(runtime.database)
    server = AizkMCP(
        runtime.auth,
        runtime.store,
        runtime.uploads,
        runtime.artifacts.intake,
        runtime.settings,
        wake=configured_worker_wake(runtime.settings),
    )
    return Mangum(server.http_app(path="/mcp", stateless_http=True), lifespan="auto")


def mcp_handler(event: Mapping[str, Json], context: LambdaContext) -> dict[str, Json]:
    """Adapt one API Gateway HTTP event to the cached MCP ASGI application."""
    return mcp_application()(dict(event), context)
