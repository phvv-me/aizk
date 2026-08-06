from datetime import datetime
from typing import Annotated, Literal

from patos import FrozenModel
from pydantic import UUID7, Field, JsonValue, WithJsonSchema

from ..provenance import EpistemicKind, Stance


class ExtractedEntity(FrozenModel):
    """An entity proposed by extraction before resolution to a stored node."""

    name: str = Field(description="plain human-readable noun phrase, never an identifier")
    type: str
    suggested_type: str | None = None
    attributes: dict[str, JsonValue] = {}


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
    stance: Stance = Stance.settled
    correcting: bool = Field(
        default=False, description="the supporting sentence announces a correction"
    )
    derived_by: str | None = Field(
        default=None, description="extractor that produced this fact, absent when the author did"
    )

    @property
    def contests(self) -> bool:
        """Whether this fact challenges something memory may already hold.

        Either the source sentence behind it announces a correction, which grounding reads
        deterministically, or extraction itself found the source disputing or refuting the
        claim. A contesting fact is never settled by similarity, because the live claim
        nearest to it is exactly the one it may be disproving.
        """
        return self.correcting or self.stance in {Stance.disputed, Stance.refuted}


class Extraction(FrozenModel):
    """The structural graph slice extracted from one text span."""

    entities: list[ExtractedEntity]
    facts: list[TimedFact]


class ConsolidationVerdict(FrozenModel):
    """How one new fact relates to the current matching facts."""

    action: Literal["ADD", "UPDATE", "NOOP", "REFUTE"]
    supersedes: Annotated[UUID7, WithJsonSchema({"type": "string"})] | None = None

    @property
    def contradicted(self) -> UUID7 | None:
        """The live claim this verdict disproves, absent for every other action."""
        return self.supersedes if self.action == "REFUTE" else None


class BatchConsolidationVerdict(FrozenModel):
    """One consolidation verdict per ambiguous fact in source order."""

    verdicts: list[ConsolidationVerdict] = Field(max_length=8)
