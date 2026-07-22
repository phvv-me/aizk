import asyncio
from collections.abc import Mapping
from typing import Protocol

from loguru import logger
from mangum import Mangum

from .. import ops
from ..background.schedule import run_worker_once
from ..config import settings
from ..mcp.server import AizkMCP
from ..runtime import Runtime
from ..store.mixins.base import Json
from ..usage import observe


class LambdaContext(Protocol):
    """AWS request metadata used by the serverless entrypoints."""

    aws_request_id: str


async def drain() -> int:
    """Assemble one short-lived runtime and drain one portable queue wave."""
    async with Runtime.assemble(settings) as runtime:
        observe(runtime.database)
        return await run_worker_once(runtime)


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


runtime = Runtime.assemble(settings)
observe(runtime.database)
server = AizkMCP(
    runtime.auth,
    runtime.store,
    runtime.uploads,
    runtime.artifacts.intake,
    runtime.settings,
)
mcp_handler = Mangum(server.http_app(path="/mcp", stateless_http=True), lifespan="auto")
