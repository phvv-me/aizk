import uuid
from typing import Self

from patos import FrozenModel


class JobPayload(FrozenModel):
    """Base for a durable queue job payload, giving every subclass a stable JSON wire format."""

    def encode(self) -> bytes:
        """Serialize to the bytes payload pgqueuer stores and a worker entrypoint decodes."""
        return self.model_dump_json().encode()

    @classmethod
    def decode(cls, payload: bytes) -> Self:
        """Parse a dequeued job's payload back into its typed fields.

        payload: the encoded bytes a worker's entrypoint receives.
        """
        return cls.model_validate_json(payload)


class ChunkJob(JobPayload):
    """Extraction job naming the chunk to build and the principal that owns it.

    chunk_id: chunk whose graph slice the job will build.
    principal_id: identity that owns the entities and facts the job writes.
    """

    chunk_id: uuid.UUID
    principal_id: uuid.UUID


class ProfileJob(JobPayload):
    """Profile-rebuild job naming the touched entity and the principal that owns it.

    entity_id: entity whose profile the job will rebuild.
    principal_id: identity that owns the profile.
    """

    entity_id: uuid.UUID
    principal_id: uuid.UUID


class TaskJob(JobPayload):
    """Scheduled-task job naming the principal a fanned-out pass runs for.

    principal_id: identity the fanned-out job runs its pass for.
    """

    principal_id: uuid.UUID
