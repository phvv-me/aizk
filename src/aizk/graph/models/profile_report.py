from patos import FrozenModel
from pydantic import Field


class ProfileReport(FrozenModel):
    """The LLM's portrait of one entity, before it is embedded and stored."""

    summary: str = Field(
        max_length=1200, description="one static-plus-dynamic paragraph grounded only in the facts"
    )
