from datetime import datetime

from patos import FrozenModel
from pydantic import Field

from ...provenance import EpistemicKind


class ExtractedEntity(FrozenModel):
    """An entity proposed by the extractor, before resolution to a stored node."""

    name: str = Field(description="plain human-readable noun phrase, never a slug or identifier")
    type: str
    suggested_type: str | None = None
    attributes: dict = {}


class TimedFact(FrozenModel):
    """A structural fact from the combined extraction, already dated, the candidate
    consolidation consumes."""

    subject: str
    predicate: str
    object_: str = Field(
        default="", alias="object", description="object entity name, empty if none"
    )
    statement: str = Field(
        description="self-contained sentence that stands without the source text"
    )
    quote: str | None = Field(
        default=None, description="verbatim source excerpt supporting this fact, when quoted"
    )
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    kind: EpistemicKind = EpistemicKind.world


class Extraction(FrozenModel):
    """The structural graph slice from one text span, the output of the combined single call."""

    entities: list[ExtractedEntity]
    facts: list[TimedFact]
