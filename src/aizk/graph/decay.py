import uuid

from loguru import logger

from ..config import settings
from ..store import FactClaim, acting_as


async def decay(
    principal_id: uuid.UUID | None = None,
    half_life_days: float = 90.0,
) -> int:
    """Archive the stale, rarely accessed latest claims so default recall drops them, return count.

    Scores each visible latest claim by an exponential decay of its age against half_life_days,
    lifted by how often and how recently recall has reached for it, then archives the claims that
    fall below the relevance floor by closing `recorded` and marking the row decayed in its
    attributes. Nothing is deleted, so an archived claim stays in history and an as-of query still
    sees it, it only leaves the live graph default recall reads. Returns the number archived.

    principal_id: identity whose row level security visibility scopes and owns the archival, the
        system principal when null.
    half_life_days: age in days at which an unaccessed claim's relevance halves.
    """
    principal_id = principal_id or settings.system_principal_id
    async with acting_as(principal_id) as session:
        archived = await FactClaim.archive_stale(session, half_life_days, settings.decay_floor)
    logger.info(
        "decay archived {count} stale claims below relevance {floor}",
        count=len(archived),
        floor=settings.decay_floor,
    )
    return len(archived)
