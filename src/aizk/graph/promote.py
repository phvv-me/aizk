import uuid
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..exceptions import NotVisibleError
from ..store import Chunk, Document, Group, LiveFact, Membership, acting_as
from .dedupe import claim_entity, claim_fact


async def target_groups(
    session: AsyncSession, user_id: uuid.UUID, to_scopes: str
) -> list[uuid.UUID]:
    """The target group ids named by a comma-separated scope list, vetted as writable.

    Fails fast with a clear error before any row is written. The write policies would otherwise
    refuse a copy into a scope set the promoter holds no writer role in every group of, but the
    error that surfaces from a refused write is far less legible than this early, explicit check.

    session: open session under the promoter's own visibility.
    user_id: the promoter, whose writable groups gate the target set.
    to_scopes: comma-separated names of the target groups the copy is published into.
    """
    names = [name.strip() for name in to_scopes.split(",") if name.strip()]
    groups = [await Group.named(session, name) for name in names]
    target = sorted(group.id for group in groups)
    writable = set(await session.scalars(Membership.writable_group_ids(user_id)))
    if not set(target) <= writable:
        raise ValueError(f"user {user_id} may not publish into {to_scopes!r}")
    return target


async def source_document(session: AsyncSession, document_id: uuid.UUID) -> Document:
    """The promoted document, with its chunks already loaded in their own document order.

    session: open session under the promoter's own visibility.
    document_id: source document to promote.
    """
    source = await session.get(Document, document_id, options=[selectinload(Document.chunks)])
    if source is None:
        raise NotVisibleError(f"no visible document {document_id}")
    return source


async def source_live_facts(session: AsyncSession, chunks: list[Chunk]) -> list[LiveFact]:
    """The live facts sourced from a document's own chunks, the only facts a promotion carries.

    Only the live facts travel, since promotion publishes current knowledge, not superseded
    history, and a source_chunk_id in this set excludes any claim whose chunk is gone. Reading
    `LiveFact` also skips the `do_orm_execute` listener's `FactClaim`-keyed curation gate (a
    distinct mapped class never picks it up), so a still-pending source claim travels too,
    re-stamped fresh for the target scope set below like any other curated write.

    session: open session under the promoter's own visibility.
    chunks: the source document's own chunks, whose ids scope the read.
    """
    return list(
        await session.scalars(
            select(LiveFact).where(LiveFact.source_chunk_id.in_([chunk.id for chunk in chunks]))
        )
    )


def copied_chunks(
    chunks: list[Chunk], user_id: uuid.UUID, target: list[uuid.UUID]
) -> dict[uuid.UUID, Chunk]:
    """Fresh copies of a document's chunks in the target scope set, keyed by their source chunk id.

    Each source chunk maps to its fresh copy so a promoted claim re-points its provenance at the
    new chunk rather than the original, keeping the copy a self-contained subgraph.

    chunks: the source document's own chunks, in document order.
    user_id: the promoter, owner of every copy.
    target: the target group set every copy is published into.
    """
    return {
        chunk.id: Chunk(
            ord=chunk.ord,
            text=chunk.text,
            tokens=chunk.tokens,
            embedding=chunk.embedding,
            owner_id=user_id,
            scopes=target,
        )
        for chunk in chunks
    }


async def claim_promoted_entities(
    session: AsyncSession, facts: list[LiveFact], user_id: uuid.UUID, target: list[uuid.UUID]
) -> None:
    """Claim, in the target scope set, every entity a promoted fact's subject or object names.

    So a target-scope member reads the entity's name and type directly rather than only reaching
    its id through the fact.

    session: open session under the promoter's own visibility.
    facts: the live facts being promoted.
    user_id: the promoter, owner of the new claims.
    target: the target group set the claims are published into.
    """
    entity_ids = {fact.subject_id for fact in facts} | {
        fact.object_id for fact in facts if fact.object_id is not None
    }
    for entity_id in entity_ids:
        await claim_entity(session, entity_id, user_id, target)


async def claim_promoted_facts(
    session: AsyncSession,
    facts: list[LiveFact],
    copies: dict[uuid.UUID, Chunk],
    user_id: uuid.UUID,
    target: list[uuid.UUID],
    stamp: datetime | None,
) -> None:
    """Claim every promoted fact's already-global content in the target scope set.

    Fact content is already global and deduplicated, so promotion never copies it, only claims the
    same content in the target scope set, each new claim's own promoted_from pointing back at the
    source claim it was promoted from.

    session: open session under the promoter's own visibility.
    facts: the live facts being promoted.
    copies: source chunk id to its fresh copy, from copied_chunks.
    user_id: the promoter, owner of the new claims.
    target: the target group set the claims are published into.
    stamp: this promotion's own reviewed_at stamp, from Group.review_stamp.
    """
    for fact in facts:
        origin = copies[fact.source_chunk_id] if fact.source_chunk_id else None
        if origin is None:  # pragma: no cover - the source_chunk_id IN filter guarantees a copy
            continue
        await claim_fact(
            session,
            fact.content_id,
            user_id,
            target,
            valid=fact.valid,
            source_chunk_id=origin.id,
            attributes=dict(fact.attributes),
            reviewed_at=stamp,
            promoted_from=fact.id,
        )


async def promote(
    document_id: uuid.UUID,
    to_scopes: str,
    user_id: uuid.UUID | None = None,
) -> int:
    """Copy a document and its chunks into a wider scope set, and claim its facts there too.

    References flow one way up the lattice, private to team to org and never down. Promotion reads
    the source under the promoter's visibility and writes a fresh copy owned by the promoter into
    the target group set, never mutating the source. The document copy carries promoted_from
    pointing back to the original for provenance. A curated target reviews the claimed facts
    immediately only when the promoter already holds admin standing in every curated group the
    target set names, otherwise they land pending like any other curated write.

    document_id: source document to promote, read under the promoter's visibility.
    to_scopes: comma-separated names of the target groups the copy is published into, one step
        wider than the source.
    user_id: the promoter, owner of the new copy and the user the writes act as, the
        system user when null.
    """
    user_id = user_id or settings.system_user_id
    async with acting_as(user_id) as session:
        target = await target_groups(session, user_id, to_scopes)
        source = await source_document(session, document_id)
        # resolved once for the whole copy, since every promoted claim shares the same target
        # scope set and promoter.
        stamp = await Group.review_stamp(session, tuple(target), user_id)
        chunks = source.chunks
        facts = await source_live_facts(session, chunks)
        copies = copied_chunks(chunks, user_id, target)
        session.add(
            Document(
                kind=source.kind,
                title=source.title,
                content_hash=source.content_hash,
                owner_id=user_id,
                scopes=target,
                promoted_from=source.id,
                chunks=list(copies.values()),
            )
        )
        await session.flush()
        await claim_promoted_entities(session, facts, user_id, target)
        await claim_promoted_facts(session, facts, copies, user_id, target, stamp)
    promoted = 1 + len(chunks) + len(facts)
    logger.info("promoted document {} into {} as {} rows", document_id, to_scopes, promoted)
    return promoted
