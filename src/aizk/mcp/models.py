import uuid

from patos import FrozenModel


class WriteResult(FrozenModel):
    """The id one write verb wrote, the common return for remember and reference.

    id: identity of the row the write landed as.
    """

    id: uuid.UUID


class MoveResult(FrozenModel):
    """How many documents one move call re-scoped, and where they now live.

    moved: documents re-scoped, each carrying its chunks and derived facts with it.
    scopes: comma-separated org names they now live under, empty when moved back to private.
    """

    moved: int
    scopes: str
