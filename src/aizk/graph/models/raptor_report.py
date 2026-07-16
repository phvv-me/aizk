from patos import FrozenModel
from pydantic import Field


class RaptorReport(FrozenModel):
    """The LLM's rollup of one cluster of lower-level summaries, before it is embedded and
    stored."""

    label: str = Field(
        max_length=96,
        description="short human-readable name for the broader theme",
    )
    summary: str = Field(
        max_length=1200,
        description="one paragraph grounded only in the child summaries shown",
    )
