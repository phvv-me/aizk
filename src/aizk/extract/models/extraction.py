from datetime import datetime

from patos import FrozenModel
from pydantic import Field

from ..ontology import EntityType, RelationType


class LLMEntity(FrozenModel):
    """One entity in the combined extraction call's compact wire schema.

    Short single-letter keys hold the call's own output near the ~250-token budget the combined
    call targets, since every entity and fact in a chunk repeats these keys; both fields here are
    categorical (a name, an enum) rather than free text, the case `LLMFact`'s own docstring found
    a small model still follows reliably at one letter. Converted to `ExtractedEntity` immediately
    after parsing; nothing downstream of `combined_extract` ever sees this shape.

    n: plain human-readable noun phrase, never a slug or identifier.
    t: ontology entity type.
    """

    n: str = Field(description="plain human-readable noun phrase, never a slug or identifier")
    t: EntityType


class LLMFact(FrozenModel):
    """One fact in the combined extraction call's compact wire schema.

    Entities, relations, and an optional date all come back from the one call this schema shapes.
    `s`/`p`/`o` stay short keys for the token-budget reason `LLMEntity` explains, but `statement`
    and `date` keep their full names: measured against Gemma 4 E2B, a two-letter key on a
    free-text field (`st`/`d`) collapsed the model's own output to the literal string `"true"` on
    every fact, while the categorical/name fields (`s`/`p`/`o`/`n`/`t`) stayed reliable at one
    letter. A small model apparently needs a semantically anchored key to generate free text under
    structured decoding, even though it never needed one for a name or an enum value, so the two
    prose fields spend the extra tokens and the rest of the schema stays compact. Converted to
    `TimedFact` immediately after parsing, `extract.dating.resolve_valid_from` turning `date` into
    a real timestamp with no LLM call of its own.

    s: subject entity name.
    p: ontology relation type.
    o: object entity name, empty for a unary fact.
    statement: self-contained sentence that stands without the source text, never an echo of it.
    date: the date the source text explicitly names for this fact, ISO 8601 or free text, null
        when the text names none.
    """

    s: str
    p: RelationType
    o: str = ""
    statement: str = Field(
        description="self-contained sentence that stands without the source text"
    )
    date: str | None = None


class LLMExtraction(FrozenModel):
    """The combined extraction call's compact wire schema, entities and facts in one shot.

    e: the typed nodes mentioned in the span.
    f: the structural edges asserted by the span, each carrying its own optional date.
    """

    e: list[LLMEntity]
    f: list[LLMFact]


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


class TimedFact(FrozenModel):
    """A structural fact from the combined extraction, already dated, the candidate consolidation
    consumes.

    subject: subject entity name.
    predicate: ontology relation type. Pydantic renders `RelationType` as a json-schema enum
        natively, the lever that makes an off-ontology predicate impossible, and
        vLLM/Ollama/Cerebras's grammar-constrained decoding keeps every value inside it.
    object_: object entity name, empty for a unary fact.
    statement: self-contained natural-language rendering of the fact.
    valid_from: start of the world-time window when the statement holds, resolved by
        `extract.dating.resolve_valid_from` and its document-timestamp fallback from the model's
        own date, a date found in the statement text, or the source document's own timestamp, in
        that order, so a fact is never left undated.
    valid_to: end of the world-time window, null while still holding; nothing in this pipeline
        ever sets it, since only a single reference date is resolved per fact.
    """

    subject: str
    predicate: RelationType
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
