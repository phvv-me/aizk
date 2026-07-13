import uuid

from patos import FrozenModel


class Node(FrozenModel):
    """One node of the RAPTOR tree held in memory while a level is being built."""

    entity_id: uuid.UUID
    label: str
    summary: str
    embedding: list[float]
