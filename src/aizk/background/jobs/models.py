from pydantic import UUID7

from ...common.queue import QueuePayload
from ...types import Scopes


class ChunkJob(QueuePayload):
    """Graph projection request for one chunk and exact scope set."""

    chunk_id: UUID7
    scopes: Scopes


class MaintenanceJob(QueuePayload):
    """Scheduled maintenance request for one exact scope set."""

    scopes: Scopes
