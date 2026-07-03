from patos import FrozenModel
from pydantic import Field


class ProfileReport(FrozenModel):
    """The LLM's portrait of one entity, before it is embedded and stored.

    summary: one paragraph holding the entity's static identity and its current dynamic state.
    """

    summary: str = Field(
        description="one static-plus-dynamic paragraph grounded only in the facts"
    )
