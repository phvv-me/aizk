from patos import FrozenModel
from pydantic import UUID5, UUID7

from .candidate import Candidate


class RecallEvidence(FrozenModel):
    """One recall's packed candidates together with the signals it computed on the way.

    The scores and the query mentions are byproducts of retrieval that used to be thrown
    away. The egress router reads both, and reading them here is what keeps deciding
    whether a question needs the public web free rather than another model call.
    """

    candidates: tuple[Candidate, ...] = ()
    scores: dict[UUID5 | UUID7 | None, float] = {}
    mentions: tuple[str, ...] = ()
