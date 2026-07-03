from patos import FrozenModel
from pydantic import Field


class RaptorReport(FrozenModel):
    """The LLM's rollup of one cluster of lower-level summaries, before it is embedded and stored.

    label: short human-readable name for the broader theme the children share.
    summary: one paragraph describing what the merged children cover.
    """

    label: str = Field(description="short human-readable name for the broader theme")
    summary: str = Field(description="one paragraph grounded only in the child summaries shown")
