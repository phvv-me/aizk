import math
import uuid

from ..config import settings
from ..extract.models import ConsolidationVerdict
from ..store import LiveFact


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length dense vectors, no server round trip.

    The non-LLM consolidation cascade's own metric, computed client-side once a subject's whole
    live-fact pool is already in hand, rather than paying one `ORDER BY <=>` query per candidate.

    a: first vector.
    b: second vector, the same width as `a`.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    magnitude = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / magnitude if magnitude else 0.0


def rank_pool(vector: list[float], pool: list[LiveFact]) -> list[tuple[LiveFact, float]]:
    """A subject's unordered live-fact pool, ranked against one candidate's own vector.

    `GraphWriter.live_facts_by_subject` fetches a subject's whole pool once for every candidate
    that names it, since a single SQL statement cannot `ORDER BY` a different query vector per
    row. This is the per-candidate ranking that reads instead of a second round trip.

    vector: the new statement's already-embedded dense vector.
    pool: the subject's visible latest claims, unordered.
    """
    scored = [
        (claim, cosine_similarity(vector, claim.embedding.to_list()))
        for claim in pool
        if claim.embedding is not None
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[: settings.similar_facts]


def decide_by_rule(
    predicate: str, object_id: uuid.UUID | None, scored: list[tuple[LiveFact, float]]
) -> ConsolidationVerdict | None:
    """Decide a candidate fact's ADD/UPDATE/NOOP verdict from cosine similarity alone, when
    possible.

    No existing claim of the same subject is a trivial ADD. A top match under
    `settings.consolidation_borderline_floor` is too dissimilar to be about the same thing,
    another trivial ADD. A top match at or above `settings.consolidation_auto_merge_threshold`
    decides deterministically. The same predicate and object is a near-duplicate NOOP, the same
    predicate with a different object is an UPDATE superseding it, and a different predicate is a
    genuinely different assertion, ADD. A top match strictly between the two thresholds is
    genuinely ambiguous and returns null, deferring to the batched borderline LLM call.

    predicate: the candidate fact's own ontology relation type.
    object_id: the candidate fact's own resolved object, null for a unary fact.
    scored: the candidate's ranked similar claims from `rank_pool`, most similar first.
    """
    if not scored:
        return ConsolidationVerdict(action="ADD")
    best, similarity = scored[0]
    if similarity < settings.consolidation_borderline_floor:
        return ConsolidationVerdict(action="ADD")
    if similarity < settings.consolidation_auto_merge_threshold:
        return None
    if best.predicate == predicate and best.object_id == object_id:
        return ConsolidationVerdict(action="NOOP")
    if best.predicate == predicate:
        return ConsolidationVerdict(action="UPDATE", supersedes=best.id)
    return ConsolidationVerdict(action="ADD")
