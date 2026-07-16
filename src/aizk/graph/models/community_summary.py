from patos import FrozenModel
from pydantic import Field


class CommunitySummary(FrozenModel):
    """The LLM's report on one detected cluster, before it is embedded and stored."""

    label: str = Field(
        max_length=96,
        description="short human-readable name for the cluster theme",
    )
    summary: str = Field(
        max_length=1200,
        description="one paragraph grounded only in the facts shown",
    )
