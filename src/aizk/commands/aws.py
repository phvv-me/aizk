import asyncio
from collections.abc import Mapping
from functools import cache

from loguru import logger
from mangum import Mangum
from mangum.types import LambdaContext

from .. import ops
from ..background.schedule import run_worker_once
from ..background.wake import configured_worker_wake
from ..config import settings
from ..mcp.server import AizkMCP
from ..runtime import Runtime
from ..store.engine import Database
from ..store.mixins.base import Json
from ..usage import observe


async def drain() -> int:
    """Assemble one short-lived runtime and drain one portable queue wave."""
    async with Runtime.assemble(settings) as runtime:
        instrument(runtime.database)
        return await run_worker_once(runtime)


@cache
def instrument(database: Database) -> None:
    """Install process tracing once while Lambda reuses a warm execution environment."""
    observe(database)


def worker_handler(
    event: Mapping[str, Json],
    context: LambdaContext,
) -> dict[str, Json]:
    """Handle an EventBridge Scheduler invocation for the portable worker."""
    logger.bind(request_id=context.aws_request_id).info("draining portable queue")
    return {"handled": asyncio.run(drain())}


def setup_handler(
    event: Mapping[str, Json],
    context: LambdaContext,
) -> dict[str, Json]:
    """Apply database migrations from an explicitly invoked deployment function."""
    logger.bind(request_id=context.aws_request_id).info("applying database setup")
    return ops.SetupReport.model_validate(asyncio.run(ops.setup())).model_dump(mode="json")


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
