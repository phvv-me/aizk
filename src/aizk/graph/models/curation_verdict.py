import uuid

from patos import FrozenModel
from pydantic import Field


class CurationVerdict(FrozenModel):
    """One judged verdict on a single pending claim, against a curated group's visible canon.

    claim: id of the pending claim this verdict judges, echoed back so the batch response aligns
        to the queue positionally and by id alike, the second check catching a model that
        reordered or dropped an entry.
    approve: whether the claim holds up against the canon shown and should join it.
    reason: one-line justification the review log carries, grounded only in the canon shown.
    """

    claim: uuid.UUID
    approve: bool
    reason: str = Field(description="one grounded sentence justifying the verdict")


class CurationReview(FrozenModel):
    """The judged verdicts for one curated group's whole pending queue.

    verdicts: one verdict per pending claim shown, each naming the claim id it judges.
    """

    verdicts: list[CurationVerdict]
