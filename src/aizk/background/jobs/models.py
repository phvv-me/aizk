from pydantic import UUID7

from ...types import Scopes
from ..queue import QueuePayload


class ChunkJob(QueuePayload):
    """Graph projection request for one chunk and exact scope set."""

    chunk_id: UUID7
    scopes: Scopes


class ArtifactConversionJob(QueuePayload):
    """Docling conversion request for one durable immutable original."""

    artifact_content_id: UUID7
    scopes: Scopes


class MaintenanceJob(QueuePayload):
    """Scheduled maintenance request for one exact scope set."""

    scopes: Scopes
