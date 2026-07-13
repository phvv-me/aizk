from patos import FrozenModel
from pydantic import Field


class CommunitySummary(FrozenModel):
    """The LLM's report on one detected cluster, before it is embedded and stored."""

    label: str = Field(description="short human-readable name for the cluster theme")
    summary: str = Field(description="one paragraph grounded only in the facts shown")
