from patos import FrozenModel

from ..extract.models import ExtractedEntity, Extraction, TimedFact
from ..ontology import System
from .naming import normalize_name


class ProjectionQuality(FrozenModel):
    """Deterministic acceptance counts for one model-proposed graph projection."""

    proposed_entities: int
    accepted_entities: int
    proposed_facts: int
    accepted_facts: int
    missing_quote: int = 0
    unsupported_quote: int = 0
    unresolved_endpoint: int = 0
    self_relation: int = 0
    generic_relation: int = 0

    @property
    def rejected_facts(self) -> int:
        """How many proposed facts failed one acceptance rule."""
        return self.proposed_facts - self.accepted_facts


class FactGrounding(FrozenModel):
    """One proposed fact and the deterministic reason it was rejected."""

    fact: TimedFact
    rejection: str | None


class GroundedProjection(FrozenModel):
    """The evidence-backed subset of one model-proposed extraction."""

    entities: list[ExtractedEntity]
    facts: list[TimedFact]
    quality: ProjectionQuality

    @classmethod
    def audit(
        cls,
        extraction: Extraction,
        source: str,
    ) -> tuple[FactGrounding, ...]:
        """Explain deterministic grounding for every proposed fact."""
        entities = {
            key: entity for entity in extraction.entities if (key := normalize_name(entity.name))
        }
        return tuple(
            FactGrounding(
                fact=fact,
                rejection=cls.rejection(
                    fact,
                    source,
                    entities,
                    normalize_name(fact.subject),
                    normalize_name(fact.object_),
                ),
            )
            for fact in extraction.facts
        )

    @classmethod
    def from_extraction(cls, extraction: Extraction, source: str) -> GroundedProjection:
        """Accept only source-grounded facts with resolved, distinct endpoints."""
        entities: dict[str, ExtractedEntity] = {}
        for entity in extraction.entities:
            if name := normalize_name(entity.name):
                entities.setdefault(name, entity)
        accepted: list[TimedFact] = []
        used: set[str] = set()
        rejected = {
            "missing_quote": 0,
            "unsupported_quote": 0,
            "unresolved_endpoint": 0,
            "self_relation": 0,
            "generic_relation": 0,
        }
        for fact in extraction.facts:
            subject = normalize_name(fact.subject)
            object_name = normalize_name(fact.object_)
            reason = cls.rejection(fact, source, entities, subject, object_name)
            if reason is not None:
                rejected[reason] += 1
                continue
            canonical_subject = entities[subject].name
            canonical_object = entities[object_name].name if object_name else ""
            accepted.append(
                fact.model_copy(update={"subject": canonical_subject, "object_": canonical_object})
            )
            used.add(subject)
            if object_name:
                used.add(object_name)
        accepted_entities = [entity for key, entity in entities.items() if key in used]
        return cls(
            entities=accepted_entities,
            facts=accepted,
            quality=ProjectionQuality(
                proposed_entities=len(extraction.entities),
                accepted_entities=len(accepted_entities),
                proposed_facts=len(extraction.facts),
                accepted_facts=len(accepted),
                **rejected,
            ),
        )

    @staticmethod
    def rejection(
        fact: TimedFact,
        source: str,
        entities: dict[str, ExtractedEntity],
        subject: str,
        object_name: str,
    ) -> str | None:
        """Return the one deterministic rejection reason for a proposed fact, if any."""
        if not fact.quote or not fact.quote.strip():
            return "missing_quote"
        if quote_interval(fact.quote, source) is None:
            return "unsupported_quote"
        if subject not in entities or (fact.object_.strip() and object_name not in entities):
            return "unresolved_endpoint"
        if object_name and subject == object_name:
            return "self_relation"
        if fact.predicate.casefold() == System.Relation.RELATED_TO:
            return "generic_relation"
        return None


def normalized_map(text: str) -> tuple[str, list[int]]:
    """Normalize text while preserving each output character's source offset.

    Markdown backticks carry presentation rather than evidence. Models commonly omit them
    from otherwise verbatim quotes, so grounding ignores them on both sides.
    """
    folded: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for offset, char in enumerate(text):
        if char == "`":
            continue
        if char.isspace():
            pending_space = bool(folded)
            continue
        if pending_space:
            folded.append(" ")
            offsets.append(offset - 1)
            pending_space = False
        for piece in char.casefold():
            folded.append(piece)
            offsets.append(offset)
    return "".join(folded), offsets


def quote_interval(quote: str | None, text: str) -> tuple[int, int] | None:
    """Locate an exact or whitespace-normalized supporting quote in source text."""
    if quote is None or not (quote := quote.strip()):
        return None
    start = text.find(quote)
    if start >= 0:
        return start, start + len(quote)
    folded_text, offsets = normalized_map(text)
    folded_quote, _ = normalized_map(quote)
    if not folded_quote:
        return None
    start = folded_text.find(folded_quote)
    if start < 0:
        return None
    last = offsets[start + len(folded_quote) - 1]
    return offsets[start], last + 1
