from loguru import logger
from sqlalchemy import func, update

from ..background.jobs.projection import enqueue_pending
from ..config import settings
from ..extract.ingest import TextSource, ingest_texts
from ..provenance import CaptureContext
from ..store import SessionItem
from ..store.identity import User
from ..types import Scopes


async def due_working_items(scopes: Scopes) -> list[SessionItem]:
    """The aged and overflow working items in one exact scope set, decided in the database."""
    async with User.system(scopes) as session:
        result = await session.exec(
            SessionItem.due_for_promotion(
                scopes,
                settings.session_promote_age_minutes,
                settings.session_promote_threshold,
            )
        )
        return list(result)


async def mark_promoted(scopes: Scopes, due: list[SessionItem]) -> None:
    """Stamp the promoted items so due_working_items never offers them again."""
    async with User.system(scopes) as session:
        await session.exec(
            update(SessionItem)
            .where(SessionItem.id.in_([item.id for item in due]))
            .values(promoted_at=func.now())
        )


async def promote_sessions(
    scopes: Scopes | None = None,
) -> int:
    """Feed a user's aged or overflow working items into the graph, return how many moved."""
    key = frozenset(scopes or (settings.system_user_id,))
    due = await due_working_items(key)
    if not due:
        return 0
    await ingest_texts(
        User.system(key),
        [
            TextSource(
                text=item.text,
                created_by=item.created_by,
                scopes=key,
                capture=CaptureContext.model_validate(item.provenance),
            )
            for item in due
        ],
    )
    await mark_promoted(key, due)
    await enqueue_pending(scopes=key)
    logger.info("promoted {} working items into graph scope {}", len(due), key)
    return len(due)
