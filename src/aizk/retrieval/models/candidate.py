from datetime import datetime
from math import ceil, floor
from typing import Self

from patos import FrozenModel
from pydantic import UUID5, UUID7, Field

from ...config import settings
from ...provenance import Stance
from ...types import Scopes
from .lane import Lane

# Each annotation renders on its own indented, labelled and backticked line.
_ANNOTATION_MARKUP = len("\n\n    Document ``")
# What a trimmed line ends with, so a reader can tell a cut excerpt from a short one.
_TRIM_MARKER = "…"


class Candidate(FrozenModel):
    """One evidence row of a context pack, cut by the Find statement.

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
    document_id: UUID7 | None = Field(
        default=None, description="source document this evidence belongs to, the share handle"
    )
    document_created_at: datetime | None = Field(
        default=None, description="when the source document entered memory"
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
    web_cache: bool = Field(
        default=False,
        description="evidence came from a cached third-party page, never from the caller",
    )
    document_expires_at: datetime | None = Field(
        default=None, description="when the source document stops being allowed to answer"
    )
    stance: Stance = Field(
        default=Stance.settled, description="how settled a derived claim is, see `Stance`"
    )

    def current_at(self, moment: datetime) -> bool:
        """Whether this evidence's source still holds, its own expiry being the authority.

        A cached page carries the expiry its freshness bucket earned when it was written or
        last refreshed, so this is exact per bucket rather than one horizon for all three.
        """
        return self.document_expires_at is None or self.document_expires_at > moment

    @property
    def document_note(self) -> str | None:
        """The source document's share handle and capture day, rendered as one terse line."""
        if self.document_id is None or self.document_created_at is None:
            return None
        return f"{self.document_id} kept {self.document_created_at:%Y-%m-%d}"

    @property
    def resource_uri(self) -> str | None:
        """The MCP resource naming the exact stored original that grounded this evidence."""
        if self.artifact_id is None or self.artifact_content_id is None:
            return None
        return f"aizk://artifacts/{self.artifact_id}/contents/{self.artifact_content_id}"

    @property
    def annotations(self) -> tuple[str, ...]:
        """Every trailing line this evidence renders beside its text.

        Packing reads these because a budget that counted only the evidence text would let
        the rendered answer overrun the caller's request by one annotation per item.
        """
        return tuple(note for note in (self.document_note, self.resource_uri) if note is not None)

    @property
    def annotation_chars(self) -> int:
        """The characters this evidence's trailing lines occupy once rendered."""
        return sum(len(note) + _ANNOTATION_MARKUP for note in self.annotations)

    @property
    def token_count(self) -> int:
        """Estimate this evidence's rendered tokens with the configured packing heuristic."""
        return ceil((len(self.line) + self.annotation_chars) / settings.find_chars_per_token)

    def trimmed(self, budget: int) -> Self:
        """This evidence cut to one token budget, marked so a reader sees the text was cut.

        Only the excerpt shortens. The annotations name the document and the stored original
        behind the evidence, and a reader handed a shortened excerpt needs those handles more
        than usual, so they keep their room and the excerpt takes what is left. They are also
        this evidence's floor: a budget too small to hold the handles alone cannot be met, and
        the marker then stands by itself rather than the evidence disappearing entirely.
        """
        room = floor((budget - 1) * settings.find_chars_per_token)
        keep = max(0, room - self.annotation_chars - len(_TRIM_MARKER))
        return self.model_copy(update={"line": self.line[:keep] + _TRIM_MARKER})

    @property
    def direct_title(self) -> str | None:
        """Return the normalized source identity only when the query names it directly."""
        return (
            self.source_title.casefold() if self.direct and self.source_title is not None else None
        )
