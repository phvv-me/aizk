import math
import uuid
from collections.abc import Sequence

from patos import FrozenModel

from ..config import settings
from ..extract.models import ConsolidationVerdict

_borderline_distance = 1.0 - settings.consolidation_borderline_floor
_automatic_distance = 1.0 - settings.consolidation_auto_merge_threshold


class FactMatch(FrozenModel):
    """The narrow current fact projection needed to consolidate one candidate."""

    id: uuid.UUID
    predicate: str
    object_id: uuid.UUID | None
    statement: str
    distance: float


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length dense vectors, no server round trip."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    magnitude = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / magnitude if magnitude else 0.0


def decide_by_rule(
    predicate: str, object_id: uuid.UUID | None, matches: Sequence[FactMatch]
) -> ConsolidationVerdict | None:
    """Decide a candidate fact's ADD/UPDATE/NOOP verdict from cosine distance alone, when
    possible."""
    if not matches:
        return ConsolidationVerdict(action="ADD")
    best = matches[0]
    if best.distance > _borderline_distance:
        return ConsolidationVerdict(action="ADD")
    if best.distance > _automatic_distance:
        return None
    if best.predicate == predicate and best.object_id == object_id:
        return ConsolidationVerdict(action="NOOP")
    if best.predicate == predicate:
        return ConsolidationVerdict(action="UPDATE", supersedes=best.id)
    return same_predicate_verdict(predicate, object_id, matches)


def same_predicate_verdict(
    predicate: str, object_id: uuid.UUID | None, matches: Sequence[FactMatch]
) -> ConsolidationVerdict | None:
    """Settle a would-be ADD by looking past the top match for a same-predicate claim."""
    match = next((candidate for candidate in matches if candidate.predicate == predicate), None)
    if match is None:
        return ConsolidationVerdict(action="ADD")
    if match.distance > _borderline_distance:
        return ConsolidationVerdict(action="ADD")
    if match.distance > _automatic_distance:
        return None
    if match.object_id == object_id:
        return ConsolidationVerdict(action="NOOP")
    return ConsolidationVerdict(action="UPDATE", supersedes=match.id)
