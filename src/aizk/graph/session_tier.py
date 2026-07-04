import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select, update

from ..background.queue import enqueue_pending
from ..config import settings
from ..extract.ingest import ingest_text
from ..store import Membership, SessionItem, acting_as


async def writable_working_items(principal_id: uuid.UUID) -> list[SessionItem]:
    """This principal's still-working items, oldest first, in scope sets it may currently write.

    Promotion reingests each item into its own scope set, so items in scope sets the principal
    can no longer write, a role demoted since the remember, stay working rather than failing.

    principal_id: identity whose working memory is read.
    """
    async with acting_as(principal_id) as session:
        return list(
            await session.scalars(
                select(SessionItem)
                .where(SessionItem.promoted_at.is_(None))
                .where(Membership.writable_scopes(SessionItem.scopes, principal_id))
                .order_by(SessionItem.created_at)
            )
        )


async def mark_promoted(principal_id: uuid.UUID, due: list[SessionItem], now: datetime) -> None:
    """Stamp the promoted items so writable_working_items never offers them again.

    principal_id: identity whose working memory is being promoted.
    due: the items just reingested into the graph.
    now: this promotion pass's own promoted_at stamp.
    """
    async with acting_as(principal_id) as session:
        await session.execute(
            update(SessionItem)
            .where(SessionItem.id.in_([item.id for item in due]))
            .values(promoted_at=now)
        )


async def promote_sessions(
    principal_id: uuid.UUID | None = None,
) -> int:
    """Feed a principal's aged or overflow working items into the graph, return how many moved.

    Reingests each due item through the same text pipeline a remember once ran so it is chunked,
    embedded, and its graph slice queued for extraction and consolidation, so the on-write
    pipeline turns settled working memory into graph facts without the recall path ever paying
    that cost.

    principal_id: identity whose working memory is promoted and that owns the written graph rows,
        the system principal when null.
    """
    principal_id = principal_id or settings.system_principal_id
    now = datetime.now(UTC)
    items = await writable_working_items(principal_id)
    due = SessionItem.due_for_promotion(
        items, now, settings.session_promote_age_minutes, settings.session_promote_threshold
    )
    if not due:
        return 0
    for item in due:
        await ingest_text(
            item.text, kind=item.kind, owner_id=principal_id, scopes=tuple(item.scopes)
        )
    await mark_promoted(principal_id, due, now)
    await enqueue_pending(principal_id=principal_id)
    logger.info("promoted {} working items into the graph for {}", len(due), principal_id)
    return len(due)
