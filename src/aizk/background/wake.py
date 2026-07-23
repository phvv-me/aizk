import asyncio
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger

from ..config import Settings

if TYPE_CHECKING:
    from mypy_boto3_lambda import LambdaClient


class WorkerWake(Protocol):
    """Wake a portable worker after a durable queue write."""

    async def wake(self) -> None: ...


class NoopWorkerWake:
    """Leave persistent local workers and scheduled recovery to their normal polling."""

    async def wake(self) -> None:
        """Complete without an external wake target."""


class LambdaWorkerWake:
    """Asynchronously invoke the bounded worker Lambda as a best-effort latency hint."""

    def __init__(self, function_name: str, client: LambdaClient | None = None) -> None:
        self.function_name = function_name
        self.client = client or boto3.client("lambda")

    async def wake(self) -> None:
        """Invoke without waiting for work, retaining the scheduled recovery fallback."""
        try:
            await asyncio.to_thread(
                self.client.invoke,
                FunctionName=self.function_name,
                InvocationType="Event",
                Payload=b"{}",
            )
        except (BotoCoreError, ClientError, OSError) as error:
            logger.warning("worker wake failed and will use scheduled recovery: {}", error)


def configured_worker_wake(config: Settings) -> WorkerWake:
    """Select direct Lambda wake only when the cloud deployment names a target."""
    if config.worker_function_name:
        return LambdaWorkerWake(config.worker_function_name)
    return NoopWorkerWake()
