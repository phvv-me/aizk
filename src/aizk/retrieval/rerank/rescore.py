from collections.abc import Mapping, Sequence

from patos import FrozenModel
from pydantic import UUID5, UUID7

from ...config import settings
from ...serving.rerank import RerankClient
from ..models import Candidate


class MeritOrder(FrozenModel):
    """Candidates in identity-aware merit order with cross-encoder diagnostic scores."""

    candidates: tuple[Candidate, ...]
    scores: dict[UUID5 | UUID7 | None, float]


def _shadowed_titles(titles: set[str]) -> set[str]:
    """Return named source titles strictly contained in another named title."""
    return {
        title for title in titles if any(title != other and title in other for other in titles)
    }


async def merit_order(candidates: Sequence[Candidate], query: str) -> MeritOrder:
    """Score every lane to depth and retain the scores for diagnostics.

    The eval strata showed no fixed lane order survives. Facts-first buries community
    and overview evidence while overview-first buries facts. The cross-encoder therefore
    orders across lanes, under the stronger identity signal of a source whose complete
    title the query explicitly names.
    """
    evidence = candidates[: settings.rerank_depth]
    scores = await RerankClient.from_settings(settings).rerank(
        query, [candidate.line for candidate in evidence]
    )
    by_evidence = dict(zip((candidate.evidence_id for candidate in evidence), scores, strict=True))
    return MeritOrder(candidates=reordered(candidates, by_evidence), scores=by_evidence)


def reordered(
    candidates: Sequence[Candidate], scores: Mapping[UUID5 | UUID7 | None, float]
) -> tuple[Candidate, ...]:
    """Put maximal named-source evidence first, then order each group by merit.

    A source title contained inside another named title is not a separate identity match. This
    prevents `JLPT N2` from shadowing `JLPT N2 Window Weekly Plan`, while unrelated titles named
    together remain peers. Ties break on `evidence_id` exactly as the statement orders them, and
    candidates beyond the scoring depth keep the statement's lane-priority order as the fallback.
    """
    direct_titles = {
        title for candidate in candidates if (title := candidate.direct_title) is not None
    }
    shadowed = _shadowed_titles(direct_titles)
    scored = sorted(
        (candidate for candidate in candidates if candidate.evidence_id in scores),
        key=lambda candidate: (
            -(candidate.direct and candidate.direct_title not in shadowed),
            -scores[candidate.evidence_id],
            candidate.evidence_id,
        ),
    )
    unscored = [candidate for candidate in candidates if candidate.evidence_id not in scores]
    return tuple(scored + unscored)
