import uuid
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import Range, insert

from ..store import EntityClaim, FactClaim
from ..store.engine import Session

# Optional claim columns supplied to the PostgreSQL upsert
type ClaimField = Range[datetime] | uuid.UUID | list[uuid.UUID] | dict | str | None


async def claim_entity(
    session: Session, content_id: uuid.UUID, created_by: uuid.UUID, scopes: list[uuid.UUID]
) -> None:
    """Idempotently insert one entity claim for an exact scope set."""
    await session.exec(
        insert(EntityClaim)
        .values(content_id=content_id, created_by=created_by, scopes=scopes)
        .on_conflict_do_nothing(index_elements=[EntityClaim.content_id, EntityClaim.scopes])
    )


async def claim_fact(
    session: Session,
    content_id: uuid.UUID,
    created_by: uuid.UUID,
    scopes: list[uuid.UUID],
    **claim_fields: ClaimField,
) -> None:
    """Idempotently insert one fact claim, a no-op against an identical already-live claim."""
    await session.exec(
        insert(FactClaim)
        .values(content_id=content_id, created_by=created_by, scopes=scopes, **claim_fields)
        .on_conflict_do_nothing(
            index_elements=[FactClaim.content_id, FactClaim.scopes, FactClaim.perspective_key],
            index_where=func.upper_inf(FactClaim.recorded),
        )
    )
