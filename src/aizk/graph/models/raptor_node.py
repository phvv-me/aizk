from patos import FrozenModel
from pydantic import UUID5


class Node(FrozenModel):
    """One node of the RAPTOR tree held in memory while a level is being built."""

    entity_id: UUID5
    label: str
    summary: str
    embedding: list[float]
