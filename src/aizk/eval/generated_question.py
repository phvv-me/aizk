from patos import FrozenModel
from pydantic import Field


class GeneratedQuestion(FrozenModel):
    """A question synthesized from one fact, the auto-built probe of recall.

    question: a natural question whose answer is the source fact.
    """

    question: str = Field(description="one natural question whose answer is the source fact")
