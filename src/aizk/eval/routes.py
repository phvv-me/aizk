from enum import StrEnum

from ..retrieval import Plan
from ..serving import classify


class Route(StrEnum):
    """The retrieval shape GLiNER2 would pick for one question, an eval-side instrument.

    Production recall stopped routing when the zero-shot classifier measured 44%
    accuracy on the eval strata, so this enum survives only to measure what query-time
    routing would have chosen and what it would have cost.
    """

    LOCAL = "specific fact or entity lookup"
    GLOBAL = "broad thematic overview or summary"
    MULTIHOP = "relationship or path between multiple entities"

    @classmethod
    async def classify(cls, query: str) -> Route:
        """Classify a query with GLiNER2's text-classification head."""
        return await classify(query, "memory retrieval route", cls)

    @property
    def plan(self) -> Plan:
        """The historical plan shape this route selected."""
        match self:
            case Route.GLOBAL:
                return Plan.overview()
            case Route.MULTIHOP:
                return Plan.multihop()
            case _:
                return Plan.focused()
