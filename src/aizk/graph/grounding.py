import re

from patos import FrozenModel

from ..extract.models import ExtractedEntity, Extraction, TimedFact
from ..ontology import System
from ..provenance import Stance
from .naming import normalize_name

# Markers of the four registers a span can speak in, each mapped to the stance it supports.
# The lists are a cheap detector, never the decision: what decides is the comparison between
# two spans read through the same detector, so a register the lists miss is missed
# symmetrically on both sides and no fact is judged by the vocabulary alone.
_REGISTERS: tuple[tuple[re.Pattern[str], Stance], ...] = (
    (
        # A reporting verb only attributes when it introduces the claim, so it is matched
        # with its complement. Bare `claim` and `note` are ordinary vocabulary here.
        re.compile(
            r"\b(?:according to|allegedly|purportedly|reportedly|supposedly)\b"
            r"|\b(?:argue[sd]?|claim(?:s|ed|ing)?|contend(?:s|ed)?|maintain(?:s|ed)?|"
            r"note[sd]?|report(?:s|ed|ing)?|said|says?|state[sd]|write[s]?|wrote) that\b",
            re.IGNORECASE,
        ),
        Stance.reported,
    ),
    (
        re.compile(
            r"\b(?:albeit|although|apparently|appears?|approximately|arguably|assum\w+|but|"
            r"caveat|conceivably|conditional|could|estimated|however|hypothes\w+|if|"
            r"inconclusive|indicat\w+|largely|likely|limitation(?:s)?|may|maybe|might|mostly|"
            r"nevertheless|nonetheless|partly|perhaps|possibly|preliminary|presumably|"
            r"probably|provided|provisional|roughly|seems?|somewhat|suggest(?:s|ed|ing)?|"
            r"tentative|though|typically|uncertain|unclear|unproven|unless|usually|whereas|"
            r"yet)\b",
            re.IGNORECASE,
        ),
        Stance.hedged,
    ),
    (
        re.compile(
            r"\b(?:contested|contradict\w*|controvers\w+|debated|disagree\w*|disputed?)\b",
            re.IGNORECASE,
        ),
        Stance.disputed,
    ),
    (
        # `invalid`, `obsolete` and `false` are ordinary technical vocabulary here, invalid
        # input, an obsolete API, a false positive rate, so none is admitted bare. Each is
        # gated behind a construction that predicates it of a claim rather than describing a
        # value: a copula directly before the adjective, or `turned out to be` before
        # `false`. Bare correction verbs need no such gate, they carry no comparable
        # everyday sense in this corpus.
        re.compile(
            r"\b(?:correct(?:ed|ing|ion)|corrigendum|debunk\w+|disprov\w+|erratum|incorrect|"
            r"invalidat\w+|misstat\w+|mistaken|overstat\w+|refut\w+|retract\w+|supersed\w+|"
            r"withdrawn)\b"
            r"|\bdoes not (?:hold|support|replicate)\b|\bno longer (?:true|holds?|valid)\b"
            r"|\b(?:was|is|were) wrong\b"
            r"|\b(?:is|was|are|were) (?:now )?(?:invalid|obsolete)\b"
            r"|\bturned out to be false\b",
            re.IGNORECASE,
        ),
        Stance.refuted,
    ),
)
# Polarity, kept apart from the registers above. A negation says nothing about how sure the
# source is, so it never moves the stance, but a quote that stops before one asserts the
# opposite of its sentence, which is the same distortion by a different route.
_NEGATIONS = re.compile(
    r"\b(?:cannot|fail(?:s|ed)? to|neither|never|nor|not)\b|n't\b",
    re.IGNORECASE,
)
_SENTENCE_BREAK = re.compile(r"[.!?\n]")


def sentence_around(text: str, start: int, end: int) -> str:
    """The smallest sentence-like span of `text` that contains the half-open range."""
    left = max((break_.end() for break_ in _SENTENCE_BREAK.finditer(text, 0, start)), default=0)
    right = _SENTENCE_BREAK.search(text, end)
    return text[left : right.end() if right is not None else len(text)]


def certainty(text: str) -> Stance:
    """The least settled register `text` speaks in, settled when it hedges nothing."""
    found = (stance for pattern, stance in _REGISTERS if pattern.search(text))
    return max(found, key=lambda stance: stance.rank, default=Stance.settled)


class Qualification(FrozenModel):
    """How one fact's certainty compares with that of the source sentence behind it.

    This is the deterministic half of the defense against hedge stripping, and it is a
    comparison rather than a lookup. A hedged sentence contains a contiguous,
    character-exact substring that asserts its confident half alone, so quote verification
    passes while the meaning inverts. Expanding the located quote to the sentence around it
    and reading both spans through the same detector is what catches that: the fact is
    judged by the gap between what its source expressed and what it kept, so a marker the
    detector does not know is invisible on both sides and costs an error in neither
    direction. A prompt instruction can be talked around; this cannot.
    """

    source: Stance = Stance.settled
    expressed: Stance = Stance.settled
    inverted: bool = False

    @classmethod
    def read(cls, fact: TimedFact, source: str) -> Qualification:
        """Read the supporting sentence and the fact's own statement through one detector.

        The statement is the side that is compared, not the quote. The quote proves the span
        exists; the statement is what gets stored, embedded, ranked and handed to a reader,
        so it is the artifact whose certainty has to match its source. A quote that spans a
        qualifier the statement then drops still leaves a flat assertion in the graph, and
        the eval measures that choice: comparing the statement alone catches half again as
        many flattenings as comparing quote and statement together, at the same cost.
        """
        interval = quote_interval(fact.quote, source)
        if interval is None:
            return cls()
        sentence = sentence_around(source, *interval)
        return cls(
            source=certainty(sentence),
            expressed=certainty(fact.statement),
            inverted=_NEGATIONS.search(sentence) is not None
            and _NEGATIONS.search(fact.statement) is None,
        )

    @property
    def stripped(self) -> bool:
        """Whether the fact reads as more certain than the sentence it was drawn from.

        Losing an attribution is not this: the claim survives intact and the stance plus the
        source excerpt beside it carry what was dropped. Losing doubt, disagreement or a
        withdrawal is, because the fact then asserts something the source never did. A
        dropped negation is the same distortion reached by inverting the claim instead of
        flattening it.
        """
        return (self.source.distorting and self.source.rank > self.expressed.rank) or self.inverted

    def settledness(self, proposed: Stance) -> Stance:
        """The stance to store: extraction's reading, never more settled than the source."""
        return proposed.at_least(self.source)

    @property
    def correcting(self) -> bool:
        """Whether the supporting sentence withdraws or corrects something asserted before."""
        return self.source is Stance.refuted


class ProjectionQuality(FrozenModel):
    """Deterministic acceptance counts for one model-proposed graph projection."""

    proposed_entities: int
    accepted_entities: int
    proposed_facts: int
    accepted_facts: int
    missing_quote: int = 0
    unsupported_quote: int = 0
    stripped_qualifier: int = 0
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
            "stripped_qualifier": 0,
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
            qualification = Qualification.read(fact, source)
            accepted.append(
                fact.model_copy(
                    update={
                        "subject": canonical_subject,
                        "object_": canonical_object,
                        "stance": qualification.settledness(fact.stance),
                        "correcting": qualification.correcting,
                    }
                )
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
        if Qualification.read(fact, source).stripped:
            return "stripped_qualifier"
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
