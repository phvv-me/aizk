from patos import FrozenModel
from pydantic import Field

from ..provenance import EpistemicKind


class WireEntity(FrozenModel):
    """Entity fields returned by the extraction wire contract."""

    n: str = Field(
        max_length=160,
        description="plain human-readable noun phrase, never a slug or identifier",
    )
    t: str = Field(max_length=64)
    suggested_type: str | None = Field(
        default=None,
        max_length=96,
        description="a more specific type name when t had to fall back to Concept",
    )


class WireFact(FrozenModel):
    """Fact fields returned by the extraction wire contract."""

    s: str = Field(max_length=160)
    p: str = Field(max_length=64)
    o: str = Field(default="", max_length=160)
    statement: str = Field(
        max_length=384,
        description="self-contained sentence that stands without source text",
    )
    quote: str = Field(
        min_length=1,
        max_length=256,
        description=(
            "one contiguous supporting substring copied character for character from the text, "
            "with no ellipses or joined passages"
        ),
    )
    date: str | None = Field(default=None, max_length=64)
    k: EpistemicKind = EpistemicKind.world


class WireExtraction(FrozenModel):
    """Combined entity and fact extraction response."""

    e: list[WireEntity] = Field(
        max_length=16,
        description="the highest-value entities, with no more than 16 per source window",
    )
    f: list[WireFact] = Field(
        max_length=8,
        description="the highest-value supported facts, with no more than 8 per source window",
    )
