from loguru import logger

from ..config import settings
from ..store import Fact
from ..store.identity import User
from ..types import Scopes


async def decay(
    scopes: Scopes | None = None,
    half_life_days: float = 90.0,
) -> int:
    """Archive the stale, rarely accessed latest claims so default recall drops them, return
    count."""
    key = frozenset(scopes or (settings.system_user_id,))
    async with User.system(key) as session:
        archived = await Fact.Claim.archive_stale(
            session, key, half_life_days, settings.decay_floor
        )
    logger.info(
        "decay archived {count} stale claims below relevance {floor}",
        count=len(archived),
        floor=settings.decay_floor,
    )
    return len(archived)
