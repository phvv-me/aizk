import uuid

from patos import FrozenModel
from pydantic import Field
from pydantic.types import UUID7

from .lane import Lane


class Candidate(FrozenModel):
    """One evidence row of a context pack, cut by the recall statement.

    The visible fields are the prompt-ready evidence and its provenance. The excluded
    `evidence_id` is the ranking identity the reranker keys its scores by between the
    statement and the packing walk. Row ids are time-ordered UUID7 while `evidence_id`
    and `created_by` stay plain UUIDs, since entity and fact content ids are
    content-addressed UUID5 and the creator is a UUID5 of the OIDC subject.
    """

    lane: Lane.Kind = Field(description="prompt section containing this evidence")
    line: str = Field(description="prompt-ready evidence text")
    fact_id: UUID7 | None = Field(default=None, description="live fact claim this line renders")
    source_chunk_id: UUID7 | None = Field(
        default=None, description="originating source chunk when one exists"
    )
    source_title: str | None = Field(
        default=None, description="human-readable originating document title"
    )
    source_uri: str | None = Field(
        default=None, description="stable originating document location"
    )
    created_by: uuid.UUID | None = Field(
        default=None, description="Logto-derived creator identity retained as provenance"
    )
    evidence_id: uuid.UUID | None = Field(default=None, exclude=True)
