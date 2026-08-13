from pydantic import UUID7

from ...types import Scopes
from ..queue import QueuePayload


class ChunkJob(QueuePayload):
    """Graph projection request for one chunk and exact scope set."""

    chunk_id: UUID7
    scopes: Scopes


class ArtifactConversionJob(QueuePayload):
    """Versioned conversion request for one durable immutable original."""

    artifact_content_id: UUID7
    scopes: Scopes
    policy: str = "converter-v2"


class ArtifactReindexJob(QueuePayload):
    """Re-chunk request for one already converted original, from its stored Markdown."""

    artifact_content_id: UUID7
    scopes: Scopes


class MaintenanceJob(QueuePayload):
    """Scheduled maintenance request for one exact scope set."""

    scopes: Scopes
