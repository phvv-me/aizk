from patos import FrozenModel
from pydantic import Field


class CommunitySummary(FrozenModel):
    """The LLM's report on one detected cluster, before it is embedded and stored.

    label: short human-readable name for the cluster's theme.
    summary: one paragraph describing what the cluster's entities and facts cover.
    """

    label: str = Field(description="short human-readable name for the cluster theme")
    summary: str = Field(description="one paragraph grounded only in the facts shown")
