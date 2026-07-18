import uuid
from collections.abc import Collection
from typing import cast

from loguru import logger
from pydantic import UUID5, UUID7
from sqlalchemy.orm import QueryableAttribute, selectinload
from sqlmodel import select

from ..exceptions import NotVisibleError
from ..store import Artifact, Chunk, Document, Fact
from ..store.engine import Session
from ..store.identity import User
from ..types import Scopes
from .dedupe import claim_entity, claim_fact


async def source_document(session: Session, document_id: UUID7) -> Document:
    """The promoted document, with its chunks already loaded in their own document order."""
    chunks = cast(QueryableAttribute[list[Chunk]], Document.chunks)
    source = await session.get(Document, document_id, options=[selectinload(chunks)])
    if source is None:
        raise NotVisibleError(f"no visible document {document_id}")
    return source


async def source_live_facts(session: Session, chunks: list[Chunk]) -> list[Fact.Live]:
    """The live facts sourced from a document's own chunks, the only facts a promotion
    carries."""
    return list(
        await session.exec(
            select(Fact.Live).where(Fact.Live.source_chunk_id.in_([chunk.id for chunk in chunks]))
        )
    )


def copied_chunks(
    chunks: list[Chunk], document_id: UUID7, user_id: UUID5, target: list[UUID5]
) -> dict[UUID7, Chunk]:
    """Fresh copies of a document's chunks in the target scope set, keyed by their source
    chunk id."""
    return {
        chunk.id: Chunk(
            document_id=document_id,
            ord=chunk.ord,
            text=chunk.text,
            lexical=chunk.lexical,
            tokens=chunk.tokens,
            provenance=dict(chunk.provenance),
            embedding=chunk.embedding,
            processed_at=chunk.processed_at,
            created_by=user_id,
            scopes=target,
        )
        for chunk in chunks
    }


async def claim_promoted_entities(
    session: Session, facts: list[Fact.Live], user_id: UUID5, target: list[UUID5]
) -> None:
    """Claim, in the target scope set, every entity a promoted fact's subject or object
    names."""
    entity_ids = {fact.subject_id for fact in facts} | {
        fact.object_id for fact in facts if fact.object_id is not None
    }
    for entity_id in entity_ids:
        await claim_entity(session, entity_id, user_id, target)


async def claim_promoted_facts(
    session: Session,
    facts: list[Fact.Live],
    copies: dict[UUID7, Chunk],
    user_id: UUID5,
    target: list[UUID5],
) -> None:
    """Claim every promoted fact's already-global content in the target scope set."""
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
            perspective_key=fact.perspective_key,
            promoted_from=fact.id,
        )


async def promote(document_ids: Collection[UUID7], scopes: Scopes, user: User) -> int:
    """Share visible documents into one authorized scope set as provenance-linked copies."""
    target = sorted(scopes)
    promoted = 0
    async with user as session:
        for document_id in document_ids:
            source = await source_document(session, document_id)
            existing = await session.exec(
                select(Document.id).where(
                    Document.promoted_from == source.id,
                    Document.scopes == target,
                )
            )
            if existing.first() is not None:
                continue
            chunks = source.chunks
            facts = await source_live_facts(session, chunks)
            promoted_id = uuid.uuid7()
            copies = copied_chunks(chunks, promoted_id, user.id, target)
            artifact_id, artifact_content_id = await Artifact.share(
                session, source, user.id, target
            )
            session.add(
                Document(
                    id=promoted_id,
                    title=source.title,
                    subject_type=source.subject_type,
                    source_uri=source.source_uri,
                    observed_at=source.observed_at,
                    expires_at=source.expires_at,
                    artifact_id=artifact_id,
                    artifact_content_id=artifact_content_id,
                    content_hash=source.content_hash,
                    created_by=user.id,
                    scopes=target,
                    promoted_from=source.id,
                    chunks=list(copies.values()),
                )
            )
            await session.flush()
            await claim_promoted_entities(session, facts, user.id, target)
            await claim_promoted_facts(session, facts, copies, user.id, target)
            promoted += 1
    logger.info("shared {} documents into {}", promoted, target)
    return promoted
