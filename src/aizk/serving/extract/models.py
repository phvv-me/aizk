from pydantic import BaseModel, Field


class Span(BaseModel):
    """One GLiNER extraction span with grounding and confidence."""

    text: str
    start: int
    end: int
    confidence: float


class Relation(BaseModel):
    """One directional GLiNER relation between grounded spans."""

    head: Span
    tail: Span


class GraphRequest(BaseModel):
    """Ontology schema and text for one GLiNER graph extraction."""

    text: str
    entity_types: dict[str, str]
    relation_types: dict[str, str]
    threshold: float = 0.5


class GraphResponse(BaseModel):
    """Grounded entities and relations returned by GLiNER in one pass."""

    entities: dict[str, list[Span]] = Field(default_factory=dict)
    relation_extraction: dict[str, list[Relation]] = Field(default_factory=dict)
