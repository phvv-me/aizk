from typing import Annotated

from patos import FrozenModel
from pydantic import Field


class Observation(FrozenModel):
    """One higher-level observation the reflective pass derived from the stored facts."""

    statement: str = Field(
        max_length=384,
        description="a self-contained insight grounded only in the facts shown",
    )
    significance: Annotated[float, Field(ge=0, le=1)] = Field(
        description="how much the insight adds beyond the facts, from 0 to 1"
    )
