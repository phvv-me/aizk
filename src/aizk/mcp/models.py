import uuid

from patos import FrozenModel


class WriteResult(FrozenModel):
    """The id one write verb wrote, the common return for remember and reference."""

    id: uuid.UUID


class ShareResult(FrozenModel):
    """How many documents one share call copied and their destination names."""

    shared: int
    scopes: tuple[str, ...]
