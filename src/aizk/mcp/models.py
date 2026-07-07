import uuid

from patos import FrozenModel


class WriteResult(FrozenModel):
    """The id one write verb wrote, the common return for remember and reference.

    id: identity of the row the write landed as.
    """

    id: uuid.UUID


class PendingFact(FrozenModel):
    """One curated group's unreviewed fact awaiting a group admin's approval.

    id: identity of the pending claim.
    owner_id: principal that authored the claim.
    predicate: ontology relation type the fact asserts.
    statement: self-contained natural-language rendering of the fact.
    """

    id: uuid.UUID
    owner_id: uuid.UUID
    predicate: str
    statement: str


class ReviewResult(FrozenModel):
    """How many of a curated group's pending facts one approve or reject call changed.

    group: name of the curated group the facts belong to.
    count: pending facts approved or rejected.
    """

    group: str
    count: int
