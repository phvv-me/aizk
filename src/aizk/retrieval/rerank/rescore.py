import uuid
from collections.abc import Mapping, Sequence

from ...config import settings
from ...serving import rerank
from ..models import Candidate


async def rescore(candidates: Sequence[Candidate], query: str) -> tuple[Candidate, ...]:
    """The cross-encoder pass between the recall statement and the packing walk.

    candidates: the statement's candidate cut in arrival order.
    query: the question the cross-encoder scores each evidence line against.

    Scores every lane down to the configured depth and returns the whole cut in merit
    order. The eval strata showed no fixed lane order survives: facts-first buries
    community and overview evidence while overview-first buries facts, so the
    cross-encoder's judgment orders the pack and lane priority only shapes which
    candidates reach the scoring depth.
    """
    evidence = candidates[: settings.rerank_depth]
    scores = await rerank(query, [candidate.line for candidate in evidence])
    by_evidence = dict(zip((candidate.evidence_id for candidate in evidence), scores, strict=True))
    return reordered(candidates, by_evidence)


def reordered(
    candidates: Sequence[Candidate], scores: Mapping[uuid.UUID | None, float]
) -> tuple[Candidate, ...]:
    """Scored candidates first in descending score order, the rest in arrival order.

    Ties break on `evidence_id` exactly as the statement orders them, and candidates
    beyond the scoring depth keep the statement's lane-priority order as the fallback.
    """
    scored = sorted(
        (candidate for candidate in candidates if candidate.evidence_id in scores),
        key=lambda candidate: (-scores[candidate.evidence_id], candidate.evidence_id),
    )
    unscored = [candidate for candidate in candidates if candidate.evidence_id not in scores]
    return tuple(scored + unscored)
