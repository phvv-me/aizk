from datetime import datetime

from pydantic import UUID5, UUID7, JsonValue
from sqlalchemy.dialects.postgresql import insert

from ..store import Entity, Fact
from ..store.engine import Session

# Optional claim columns supplied to the PostgreSQL upsert
type ClaimField = datetime | UUID5 | UUID7 | list[UUID5] | dict[str, JsonValue] | str | None


async def claim_entity(
    session: Session, content_id: UUID5, created_by: UUID5, scopes: list[UUID5]
) -> None:
    """Idempotently insert one entity claim for an exact scope set."""
    await Entity.Claim.claim_all(session, [content_id], created_by, frozenset(scopes))


async def claim_fact(
    session: Session,
    content_id: UUID5,
    created_by: UUID5,
    scopes: list[UUID5],
    **claim_fields: ClaimField,
) -> None:
    """Idempotently insert one fact claim, a no-op against an identical already-live claim."""
    await session.exec(
        insert(Fact.Claim)
        .values(content_id=content_id, created_by=created_by, scopes=scopes, **claim_fields)
        .on_conflict_do_nothing(
            index_elements=[Fact.Claim.content_id, Fact.Claim.scopes, Fact.Claim.perspective_key],
            index_where=Fact.Claim.recorded_to.is_(None),
        )
    )
