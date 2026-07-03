import uuid

from patos import FrozenModel


class Node(FrozenModel):
    """One node of the RAPTOR tree held in memory while a level is being built.

    entity_id: the stored summary entity this node is, the part_of edges link against it.
    label: the node's short theme name, also the entity name.
    summary: the node's paragraph, the text its embedding ranks on.
    embedding: the dense vector of the summary, what the clustering of the next level reads.
    """

    entity_id: uuid.UUID
    label: str
    summary: str
    embedding: list[float]
