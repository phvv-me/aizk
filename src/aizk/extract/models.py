from datetime import datetime
from typing import Annotated, Literal

from patos import FrozenModel
from pydantic import UUID7, Field, WithJsonSchema

from ..provenance import EpistemicKind


class ExtractedEntity(FrozenModel):
    """An entity proposed by extraction before resolution to a stored node."""

    name: str = Field(description="plain human-readable noun phrase, never an identifier")
    type: str
    suggested_type: str | None = None
    attributes: dict = {}


class TimedFact(FrozenModel):
    """A dated structural fact ready for consolidation."""

    subject: str
    predicate: str
    object_: str = Field(default="", alias="object", description="object name when present")
    statement: str = Field(description="self-contained sentence that stands without the source")
    quote: str | None = Field(default=None, description="supporting verbatim source excerpt")
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    kind: EpistemicKind = EpistemicKind.world


class Extraction(FrozenModel):
    """The structural graph slice extracted from one text span."""

    entities: list[ExtractedEntity]
    facts: list[TimedFact]


class ConsolidationVerdict(FrozenModel):
    """How one new fact relates to the current matching facts."""

    action: Literal["ADD", "UPDATE", "NOOP"]
    supersedes: Annotated[UUID7, WithJsonSchema({"type": "string"})] | None = None


class BatchConsolidationVerdict(FrozenModel):
    """One consolidation verdict per ambiguous fact in source order."""

    verdicts: list[ConsolidationVerdict] = Field(max_length=8)
