from datetime import datetime

from patos import FrozenModel


class FactHit(FrozenModel):
    """A single graph-search result, one time-stamped fact.

    statement: self-contained natural-language rendering of the fact.
    predicate: ontology relation type the fact asserts.
    score: fused relevance score, higher is better.
    valid_from: start of the world-time window when the statement holds, when known.
    valid_to: end of the world-time window, null while still holding.
    """

    statement: str
    predicate: str
    score: float
    valid_from: datetime | None
    valid_to: datetime | None
