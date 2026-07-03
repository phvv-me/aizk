import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select, update

from ..background.queue import enqueue_pending
from ..config import settings
from ..extract.ingest import ingest_text
from ..store import Membership, SessionItem, acting_as


async def promote_sessions(
    principal_id: uuid.UUID | None = None,
) -> int:
    """Feed a principal's aged or overflow working items into the graph, return how many moved.

    Reads the still-working items oldest first, selects those due by age or the working cap, then
    reingests each through the same text pipeline a remember once ran so it is chunked, embedded,
    and its graph slice queued for extraction and consolidation. Each promoted item is stamped so
    it leaves the working set, and one enqueue drains the new chunks, so the on-write pipeline
    turns settled working memory into graph facts without the recall path ever paying that cost.

    principal_id: identity whose working memory is promoted and that owns the written graph rows,
        the system principal when null.
    """
    principal_id = principal_id or settings.system_principal_id
    now = datetime.now(UTC)
    async with acting_as(principal_id) as session:
        # promotion reingests each item into its own scope, so items in scopes the principal can
        # no longer write, a role demoted since the remember, stay working rather than failing.
        items = list(
            await session.scalars(
                select(SessionItem)
                .where(SessionItem.promoted_at.is_(None))
                .where(Membership.writable_scope(SessionItem.scope, principal_id))
                .order_by(SessionItem.created_at)
            )
        )
    due = SessionItem.due_for_promotion(
        items, now, settings.session_promote_age_minutes, settings.session_promote_threshold
    )
    if not due:
        return 0
    for item in due:
        await ingest_text(item.text, kind=item.kind, owner_id=principal_id, scope=item.scope)
    async with acting_as(principal_id) as session:
        await session.execute(
            update(SessionItem)
            .where(SessionItem.id.in_([item.id for item in due]))
            .values(promoted_at=now)
        )
    await enqueue_pending(principal_id=principal_id)
    logger.info("promoted {} working items into the graph for {}", len(due), principal_id)
    return len(due)
