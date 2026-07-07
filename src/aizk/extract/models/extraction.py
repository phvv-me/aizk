from datetime import datetime

from patos import FrozenModel
from pydantic import Field

# `LLMEntity`/`LLMFact`/`LLMExtraction`, the combined extraction call's compact wire schema, used
# to live here as static classes typed directly against `EntityType`/`RelationType`. They are now
# built fresh by `extract.ontology.cache._wire_schema` from whatever the live `entity_kind`/
# `relation_kind` catalog currently allows, since a class body's field annotations resolve at
# import time and this schema's own vocabulary only exists once the database has been read.
# `ExtractedEntity`/`TimedFact` below stay static, ordinary `str` fields, since by the time
# anything constructs one the wire schema has already validated the value against the live
# catalog, so a second enum-typed wall here would only repeat a check already made.


class ExtractedEntity(FrozenModel):
    """An entity proposed by the extractor, before resolution to a stored node.

    name: canonical surface form as a plain noun phrase, never a slug or identifier.
    type: entity kind name, already validated against the live catalog by the wire schema that
        produced it.
    suggested_type: a more specific type name the extractor offered when `type` had to fall back
        to Concept, `graph.ontology_growth.resolve_suggested_type`'s own input, null when `type`
        was never Concept or the model had no better guess.
    attributes: free-form structured detail extracted alongside the entity.
    """

    name: str = Field(description="plain human-readable noun phrase, never a slug or identifier")
    type: str
    suggested_type: str | None = None
    attributes: dict = {}


class TimedFact(FrozenModel):
    """A structural fact from the combined extraction, already dated, the candidate consolidation
    consumes.

    subject: subject entity name.
    predicate: relation kind name, already validated against the live catalog by the wire schema
        that produced it.
    object_: object entity name, empty for a unary fact.
    statement: self-contained natural-language rendering of the fact.
    valid_from: start of the world-time window when the statement holds, resolved by
        `extract.dating.resolve_valid_from` and its document-timestamp fallback from the model's
        own date, a date found in the statement text, or the source document's own timestamp, in
        that order, so a fact is never left undated.
    valid_to: end of the world-time window, null while still holding. Nothing in this pipeline
        ever sets it, since only a single reference date is resolved per fact.
    """

    subject: str
    predicate: str
    object_: str = Field(
        default="", alias="object", description="object entity name, empty if none"
    )
    statement: str = Field(
        description="self-contained sentence that stands without the source text"
    )
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class Extraction(FrozenModel):
    """The structural graph slice from one text span, the output of the combined single call.

    entities: the typed nodes mentioned in the span.
    facts: the structural edges asserted by the span, each already dated.
    """

    entities: list[ExtractedEntity]
    facts: list[TimedFact]
