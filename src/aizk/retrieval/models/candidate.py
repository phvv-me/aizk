from math import ceil

from patos import FrozenModel
from pydantic import UUID5, UUID7, Field

from ...config import settings
from ...types import Scopes
from .lane import Lane


class Candidate(FrozenModel):
    """One evidence row of a context pack, cut by the recall statement.

    The visible fields are the prompt-ready evidence and its provenance. The excluded
    `evidence_id` is the ranking identity the reranker keys its scores by between the
    statement and the packing walk. Claims and source rows use time-ordered UUID7
    values. Deterministic graph content and `created_by` use UUID5 values.
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
    artifact_id: UUID7 | None = Field(
        default=None,
        description="stored original that may be fetched through an authorized MCP resource",
    )
    artifact_content_id: UUID7 | None = Field(
        default=None,
        description="exact stored original revision that grounded this evidence",
    )
    created_by: UUID5 | None = Field(
        default=None, description="Logto-derived creator identity retained as provenance"
    )
    scopes: Scopes = frozenset()
    evidence_id: UUID5 | UUID7 | None = Field(default=None, exclude=True)
    direct: bool = Field(
        default=False,
        exclude=True,
        description="source title is named completely in the query",
    )

    @property
    def token_count(self) -> int:
        """Estimate the line's tokens with the configured packing heuristic."""
        return ceil(len(self.line) / settings.recall_chars_per_token)

    @property
    def direct_title(self) -> str | None:
        """Return the normalized source identity only when the query names it directly."""
        return (
            self.source_title.casefold() if self.direct and self.source_title is not None else None
        )
