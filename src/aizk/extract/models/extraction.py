from datetime import datetime

from patos import FrozenModel
from pydantic import Field

from ..ontology import EntityType, RelationType


class ExtractedEntity(FrozenModel):
    """An entity proposed by the extractor, before resolution to a stored node.

    name: canonical surface form as a plain noun phrase, never a slug or identifier.
    type: ontology entity type. Pydantic renders `EntityType` as a json-schema enum natively, the
        lever that makes an off-ontology entity type impossible, and vLLM/Ollama/Cerebras's
        grammar-constrained decoding keeps every value inside it.
    attributes: free-form structured detail extracted alongside the entity.
    """

    name: str = Field(description="plain human-readable noun phrase, never a slug or identifier")
    type: EntityType
    attributes: dict = {}


class ExtractedFact(FrozenModel):
    """A structural triple proposed by the combined extraction, before timestamps are resolved.

    The combined node-and-edge call emits these with no valid-time at all, so date parsing never
    competes with fact extraction. A dedicated timestamp pass then turns each into a `TimedFact`.

    subject: subject entity name.
    predicate: ontology relation type. Pydantic renders `RelationType` as a json-schema enum
        natively, the lever that makes an off-ontology predicate impossible, and
        vLLM/Ollama/Cerebras's grammar-constrained decoding keeps every value inside it.
    object_: object entity name, empty for a unary fact.
    statement: self-contained natural-language rendering of the fact.
    """

    subject: str
    predicate: RelationType
    object_: str = Field(
        default="", alias="object", description="object entity name, empty if none"
    )
    statement: str = Field(
        description="self-contained sentence that stands without the source text"
    )


class Extraction(FrozenModel):
    """The structural graph slice from one text span, the output of the combined single call.

    entities: the typed nodes mentioned in the span.
    facts: the structural edges asserted by the span, dated later by the timestamp pass.
    """

    entities: list[ExtractedEntity]
    facts: list[ExtractedFact]


class FactTimestamp(FrozenModel):
    """The valid-time window the timestamp pass resolves for one fact, positioned by index.

    valid_from: start of the world-time window when the statement begins to hold, null if undated.
    valid_to: end of the world-time window when the statement stops holding, null while it holds.
    """

    valid_from: datetime | None = None
    valid_to: datetime | None = None


class TimedFact(ExtractedFact):
    """A structural fact the timestamp pass has dated, the candidate consolidation consumes.

    valid_from: start of the world-time window when the statement holds, null when undated.
    valid_to: end of the world-time window, null while still holding or undated.
    """

    valid_from: datetime | None = None
    valid_to: datetime | None = None


class TimestampResolution(FrozenModel):
    """The valid-time windows the timestamp pass returns, one per extracted fact in order.

    timestamps: the resolved windows, aligned by position to the facts handed to the pass.
    """

    timestamps: list[FactTimestamp]
