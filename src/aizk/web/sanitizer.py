import re

import httpx
from loguru import logger
from patos import FrozenFlexModel

from ..config import Settings
from ..serving.gate import MentionDetector
from .models import Refusal

# A closed class, so the pattern is exact rather than approximate. A rewritten query that
# still says "my" or "our" is still the asker's question rather than a stranger's, whatever
# else was removed from it.
_FIRST_PERSON = re.compile(r"\b(?:i|me|my|mine|myself|we|us|our|ours|ourselves)\b", re.IGNORECASE)


class QuerySanitizer(FrozenFlexModel):
    """The last word on whether a rewritten query may leave the machine.

    Everything here runs after the model has spoken and none of it trusts what the model
    said. A planner asked to strip private detail is still a planner, and the one thing a
    privacy boundary must never do is take the word of the component it is guarding. So the
    rewritten query is checked three independent ways, and any one of them refusing ends the
    call.

    The first is the closed first-person pronoun class, which no public question needs. The
    second is a literal lowered-substring check against the names the caller actually stores,
    which catches the exact failure the model is most likely to make, keeping a project or
    person name it did not recognise as private. The third is the deployed GLiNER detector
    run over the union of the private-context labels and its own personal-information
    labels, at a threshold set low on purpose, because a false refusal costs one web call
    while a false pass costs a name.
    """

    gate: MentionDetector
    roster: frozenset[str]
    labels: tuple[str, ...]
    threshold: float
    roster_min_chars: int

    @classmethod
    def build(
        cls, config: Settings, gate: MentionDetector, roster: frozenset[str]
    ) -> QuerySanitizer:
        """Bind the deployed detector and this caller's roster into one checker."""
        return cls(
            gate=gate,
            roster=roster,
            labels=config.web_search_detector_labels,
            threshold=config.web_search_detector_threshold,
            roster_min_chars=config.web_search_roster_min_chars,
        )

    @property
    def guarded(self) -> frozenset[str]:
        """The roster names long enough to be identifying rather than incidental.

        A two or three letter name is a fragment of ordinary English far more often than it
        is a private identity, and refusing every query containing one would refuse nearly
        all of them, so the substring check ignores those and leaves them to the detector.
        """
        return frozenset(name for name in self.roster if len(name) >= self.roster_min_chars)

    async def refuses(self, rewritten: str) -> Refusal | None:
        """The refusal this query earns, or nothing at all when it may be sent.

        Failure is refusal here too. A detector that cannot be reached leaves the query
        unchecked, and an unchecked query is exactly the one that must not be sent.
        """
        lowered = rewritten.lower()
        if _FIRST_PERSON.search(rewritten):
            logger.info("web egress refused: the rewritten query still speaks in first person")
            return Refusal.sanitizer_refused
        if any(name in lowered for name in self.guarded):
            logger.info("web egress refused: the rewritten query still names a stored entity")
            return Refusal.sanitizer_refused
        try:
            found = await self.gate.mentions(rewritten, self.labels, self.threshold)
        except (httpx.HTTPError, OSError, ValueError) as unreachable:
            logger.warning("web egress refused: the detector is unreachable ({})", unreachable)
            return Refusal.sanitizer_refused
        if found:
            logger.info("web egress refused: the detector found identifying detail")
            return Refusal.sanitizer_refused
        return None
