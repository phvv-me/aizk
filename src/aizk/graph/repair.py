from loguru import logger
from pydantic import UUID5
from sqlalchemy import delete, or_, update
from sqlalchemy.orm import aliased
from sqlmodel import select

from ..config import settings
from ..ontology import System
from ..store import Entity, Fact
from ..store.engine import Session
from ..store.identity import User
from ..types import Scopes
from .naming import normalize_name


def redirect_entity(
    redirect: dict[UUID5, UUID5 | None], entity_id: UUID5 | None
) -> tuple[UUID5 | None, bool]:
    """Resolve one subject or object id through the duplicate-to-canonical redirect map."""
    if entity_id is None:
        return None, False
    if entity_id not in redirect:
        return entity_id, False
    replacement = redirect[entity_id]
    return replacement, replacement is None


def claim_row(claim: Fact.Claim, content_id: UUID5) -> dict:
    """One claim's full column set as a plain dict, content_id re-pointed at the corrected
    row."""
    return {
        "id": claim.id,
        "content_id": content_id,
        "created_by": claim.created_by,
        "scopes": claim.scopes,
        "valid": claim.valid,
        "recorded": claim.recorded,
        "last_accessed": claim.last_accessed,
        "access_count": claim.access_count,
        "attributes": claim.attributes,
        "perspective_key": claim.perspective_key,
        "source_chunk_id": claim.source_chunk_id,
        "promoted_from": claim.promoted_from,
    }


async def snapshot_claims(session: Session, content_id: UUID5) -> list[dict]:
    """Read and expunge a fact content's whole claim history ahead of its cascading delete."""
    claims = list(
        await session.exec(
            select(Fact.Claim)
            .where(Fact.Claim.content_id == content_id)
            .execution_options(**{settings.skip_live_gate: True})
        )
    )
    saved = [claim_row(claim, content_id) for claim in claims]
    for claim in claims:
        session.expunge(claim)
    return saved


# Suite-wide coverage loses attribution across this ad-hoc admin engine. End-to-end tests assert
# both redirect outcomes directly.
async def repoint_fact_content(  # pragma: no cover
    session: Session, content_id: UUID5, redirect: dict[UUID5, UUID5 | None]
) -> None:
    """Correct one fact content's subject or object off a duplicate, migrating its claims."""
    content = await session.get(Fact.Content, content_id)
    assert content is not None  # read as an affected id in the same admin-bypassed connection
    corrected_subject, subject_dropped = redirect_entity(redirect, content.subject_id)
    corrected_object, object_dropped = redirect_entity(redirect, content.object_id)
    if subject_dropped or object_dropped or corrected_subject is None:
        await session.delete(content)
        return
    saved = await snapshot_claims(session, content_id)
    await session.delete(content)
    await session.flush()
    session.add(
        Fact.Content(
            id=content_id,
            subject_id=corrected_subject,
            object_id=corrected_object,
            predicate=content.predicate,
            statement=content.statement,
            embedding=content.embedding,
        )
    )
    await session.flush()
    session.add_all(Fact.Claim(**row) for row in saved)


async def find_duplicates(session: Session) -> dict[UUID5, UUID5 | None]:
    """Group visible entity content by normalized name and type, return the canonical
    redirect map."""
    entities = sorted(
        await session.exec(
            select(Entity.Content).where(Entity.Content.type != System.Entity.RAPTOR_SUMMARY)
        ),
        key=lambda entity: entity.id.bytes,
    )
    canonical: dict[tuple[str, str], UUID5] = {}
    redirect: dict[UUID5, UUID5 | None] = {}
    for entity in entities:
        normalized = normalize_name(entity.name)
        keep = canonical.get((entity.type, normalized)) if normalized else None
        if normalized and keep is None:
            canonical[(entity.type, normalized)] = entity.id
            continue
        redirect[entity.id] = keep
    return redirect


async def affected_fact_ids(
    session: Session,
    redirect: dict[UUID5, UUID5 | None],
) -> list[UUID5]:
    """Fact content naming at least one duplicate the redirect map will correct or drop."""
    return list(
        await session.exec(
            select(Fact.Content.id).where(
                or_(
                    Fact.Content.subject_id.in_(redirect),
                    Fact.Content.object_id.in_(redirect),
                )
            )
        )
    )


async def migrate_entity_claims(
    session: Session, duplicate_id: UUID5, canonical_id: UUID5
) -> None:
    """Repoint a merged-away entity's own claims onto the canonical content before it is
    deleted."""
    canonical_claim = aliased(Entity.Claim)
    collides_with_canonical = (
        select(canonical_claim.id)
        .where(canonical_claim.content_id == canonical_id)
        .where(canonical_claim.scopes == Entity.Claim.scopes)
        .exists()
    )
    await session.exec(
        delete(Entity.Claim)
        .where(Entity.Claim.content_id == duplicate_id, collides_with_canonical)
        .execution_options(synchronize_session=False)
    )
    await session.exec(
        update(Entity.Claim)
        .where(Entity.Claim.content_id == duplicate_id)
        .values(content_id=canonical_id)
        .execution_options(synchronize_session=False)
    )


async def merge_duplicates(affected_ids: list[UUID5], redirect: dict[UUID5, UUID5 | None]) -> int:
    """Repoint every affected fact, migrate each duplicate's claims, and delete the duplicate
    node."""
    merged = 0
    async with User.system().owner as session:
        for content_id in affected_ids:
            await repoint_fact_content(session, content_id, redirect)
        for duplicate_id, canonical_id in redirect.items():
            entity = await session.get(Entity.Content, duplicate_id)
            if entity is not None:  # pragma: no cover - always true within a single pass
                if canonical_id is not None:
                    await migrate_entity_claims(session, duplicate_id, canonical_id)
                await session.delete(entity)
                merged += 1
    return merged


async def dedup_entities(scopes: Scopes | None = None) -> int:
    """Merge entity content sharing a normalized name and type, repoint claims, return the
    count."""
    key = frozenset(scopes or (settings.system_user_id,))
    async with User.system(key) as session:
        redirect = await find_duplicates(session)
        if not redirect:
            return 0
        affected_ids = await affected_fact_ids(session, redirect)
    merged = await merge_duplicates(affected_ids, redirect)
    logger.info("deduped {} duplicate entity content rows", merged)
    return merged
