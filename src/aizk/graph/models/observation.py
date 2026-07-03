from patos import FrozenModel
from pydantic import Field, confloat


class Observation(FrozenModel):
    """One higher-level observation the reflective pass derived from the stored facts.

    statement: a self-contained insight grounded only in the facts shown, never free self-talk.
    significance: how much the insight adds beyond the facts it rests on, the write gate reads it.
    """

    statement: str = Field(description="a self-contained insight grounded only in the facts shown")
    significance: confloat(ge=0, le=1) = Field(
        description="how much the insight adds beyond the facts, from 0 to 1"
    )
