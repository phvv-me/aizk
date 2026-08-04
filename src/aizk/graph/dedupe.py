from collections.abc import Collection, Mapping, Sequence
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


async def claim_entities(
    session: Session, content_ids: Collection[UUID5], created_by: UUID5, scopes: list[UUID5]
) -> None:
    """Idempotently claim many canonical entities inside one exact scope set."""
    await Entity.Claim.claim_all(session, sorted(content_ids), created_by, frozenset(scopes))


async def claim_facts(
    session: Session,
    claims: Sequence[Mapping[str, ClaimField]],
    created_by: UUID5,
    scopes: list[UUID5],
) -> None:
    """Idempotently insert many fact claims in one statement.

    The rows are deduplicated on the live-claim identity first, so a batch naming one
    statement twice presents the conflict target once and the upsert stays a plain insert.
    """
    unique = {
        (claim["content_id"], claim["perspective_key"]): {
            **claim,
            "created_by": created_by,
            "scopes": scopes,
        }
        for claim in claims
    }
    if not unique:
        return
    await session.exec(
        insert(Fact.Claim)
        .values(list(unique.values()))
        .on_conflict_do_nothing(
            index_elements=[Fact.Claim.content_id, Fact.Claim.scopes, Fact.Claim.perspective_key],
            index_where=Fact.Claim.recorded_to.is_(None),
        )
    )
