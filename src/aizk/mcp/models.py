from patos import FrozenModel
from pydantic import UUID7


class WriteResult(FrozenModel):
    """Identify the durable source document created or updated by `remember`."""

    id: UUID7


class ShareResult(FrozenModel):
    """Report how many provenance-linked document copies `share` created."""

    shared: int
