import math
from collections.abc import Sequence

from loguru import logger
from patos import FrozenModel
from pydantic import UUID5, UUID7

from ..config import settings
from ..extract.models import BatchConsolidationVerdict, ConsolidationVerdict, TimedFact
from ..serving.extract import LLM
from ..store import Relation

_borderline_distance = 1.0 - settings.consolidation_borderline_floor
_automatic_distance = 1.0 - settings.consolidation_auto_merge_threshold


class FactMatch(FrozenModel):
    """The narrow current fact projection needed to consolidate one candidate."""

    id: UUID7
    object_id: UUID5 | None
    statement: str
    distance: float


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length dense vectors, no server round trip."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    magnitude = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / magnitude if magnitude else 0.0


class Consolidator(FrozenModel):
    """Resolve fact candidates with deterministic rules and one model fallback batch."""

    llm: LLM

    def decide(
        self,
        policy: Relation.Policy,
        object_id: UUID5 | None,
        matches: Sequence[FactMatch],
    ) -> ConsolidationVerdict | None:
        """Apply the relation's coexistence policy when similarity is conclusive."""
        if not matches:
            return ConsolidationVerdict(action="ADD")
        if policy == Relation.Policy.state:
            if len(matches) == 1 and matches[0].object_id == object_id:
                return ConsolidationVerdict(action="NOOP")
            return ConsolidationVerdict(action="UPDATE", supersedes=matches[0].id)
        best = matches[0]
        if best.distance > _borderline_distance:
            return ConsolidationVerdict(action="ADD")
        return (
            ConsolidationVerdict(action="NOOP")
            if best.distance <= _automatic_distance and best.object_id == object_id
            else ConsolidationVerdict(action="ADD")
        )

    async def resolve(
        self,
        candidates: list[tuple[TimedFact, list[FactMatch]]],
    ) -> list[ConsolidationVerdict]:
        """Resolve every ambiguous candidate in one model call."""
        if not candidates:
            return []
        prompt = "\n\n".join(
            self._block(index, fact, existing) for index, (fact, existing) in enumerate(candidates)
        )
        resolution = await self.llm.generate(
            settings.consolidation_prompt,
            prompt,
            BatchConsolidationVerdict,
        )
        verdicts = [
            self._resolved_verdict(index, existing, resolution)
            for index, (_, existing) in enumerate(candidates)
        ]
        logger.info("batched consolidation resolved {} ambiguous facts", len(candidates))
        return verdicts

    @staticmethod
    def _block(index: int, fact: TimedFact, existing: list[FactMatch]) -> str:
        catalog = (
            "\n".join(f"  id={claim.id} statement={claim.statement}" for claim in existing)
            or "  (none)"
        )
        return f"{index}. New fact: {fact.statement}\nExisting facts.\n{catalog}"

    @staticmethod
    def _resolved_verdict(
        index: int,
        existing: list[FactMatch],
        resolution: BatchConsolidationVerdict,
    ) -> ConsolidationVerdict:
        known = {claim.id for claim in existing}
        verdict = (
            resolution.verdicts[index]
            if index < len(resolution.verdicts)
            else ConsolidationVerdict(action="ADD")
        )
        supersedes = (
            verdict.supersedes
            if verdict.action == "UPDATE" and verdict.supersedes in known
            else None
        )
        return ConsolidationVerdict(action=verdict.action, supersedes=supersedes)
