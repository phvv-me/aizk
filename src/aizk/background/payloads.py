import uuid
from typing import Self

from patos import FrozenModel

from ..types import Scopes


class JobPayload(FrozenModel):
    """Base for a durable queue job payload, giving every subclass a stable JSON wire format."""

    def encode(self) -> bytes:
        """Serialize to the bytes payload pgqueuer stores and a worker entrypoint decodes."""
        return self.model_dump_json().encode()

    @classmethod
    def decode(cls, payload: bytes) -> Self:
        """Parse a dequeued job's payload back into its typed fields."""
        return cls.model_validate_json(payload)


class ChunkJob(JobPayload):
    """Extraction job naming the chunk and exact scope set to build."""

    chunk_id: uuid.UUID
    scopes: Scopes


class ProfileJob(JobPayload):
    """Profile rebuild job naming the touched entity and exact scope set."""

    entity_id: uuid.UUID
    scopes: Scopes


class TaskJob(JobPayload):
    """Scheduled task job naming the exact scope set a pass runs for."""

    scopes: Scopes
