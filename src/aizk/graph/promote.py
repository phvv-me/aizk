import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..exceptions import NotVisibleError
from ..scopes import scopes_from_org_ids
from ..store import Chunk, Document, LiveFact, acting_as
from ..store.engine import caller_standing, session
from .dedupe import claim_entity, claim_fact


async def source_document(document_id: uuid.UUID) -> Document:
    """The promoted document, with its chunks already loaded in their own document order.

    document_id: source document to promote.
    """
    source = await session().get(Document, document_id, options=[selectinload(Document.chunks)])
    if source is None:
        raise NotVisibleError(f"no visible document {document_id}")
    return source


async def source_live_facts(chunks: list[Chunk]) -> list[LiveFact]:
    """The live facts sourced from a document's own chunks, the only facts a promotion carries.

    Only the live facts travel, since promotion publishes current knowledge, not superseded
    history, and a source_chunk_id in this set excludes any claim whose chunk is gone.

    chunks: the source document's own chunks, whose ids scope the read.
    """
    return list(
        await session().scalars(
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
    facts: list[LiveFact], user_id: uuid.UUID, target: list[uuid.UUID]
) -> None:
    """Claim, in the target scope set, every entity a promoted fact's subject or object names.

    So a target-scope member reads the entity's name and type directly rather than only reaching
    its id through the fact.

    facts: the live facts being promoted.
    user_id: the promoter, owner of the new claims.
    target: the target group set the claims are published into.
    """
    entity_ids = {fact.subject_id for fact in facts} | {
        fact.object_id for fact in facts if fact.object_id is not None
    }
    for entity_id in entity_ids:
        await claim_entity(entity_id, user_id, target)


async def claim_promoted_facts(
    facts: list[LiveFact],
    copies: dict[uuid.UUID, Chunk],
    user_id: uuid.UUID,
    target: list[uuid.UUID],
) -> None:
    """Claim every promoted fact's already-global content in the target scope set.

    Fact content is already global and deduplicated, so promotion never copies it, only claims the
    same content in the target scope set, each new claim's own promoted_from pointing back at the
    source claim it was promoted from.

    facts: the live facts being promoted.
    copies: source chunk id to its fresh copy, from copied_chunks.
    user_id: the promoter, owner of the new claims.
    target: the target group set the claims are published into.
    """
    for fact in facts:
        origin = copies[fact.source_chunk_id] if fact.source_chunk_id else None
        if origin is None:  # pragma: no cover - the source_chunk_id IN filter guarantees a copy
            continue
        await claim_fact(
            fact.content_id,
            user_id,
            target,
            valid=fact.valid,
            source_chunk_id=origin.id,
            attributes=dict(fact.attributes),
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
    pointing back to the original for provenance.

    document_id: source document to promote, read under the promoter's visibility.
    to_scopes: comma-separated names of the target groups the copy is published into, one step
        wider than the source.
    user_id: the promoter, owner of the new copy and the user the writes act as, the
        system user when null.
    """
    user_id = user_id or settings.system_user_id
    standing = scopes_from_org_ids(to_scopes)
    target = list(standing)
    # a promotion is a deliberate operator publish, so it grants itself reader and writer standing
    # in exactly the target orgs, the standing the RLS write policy checks the copy's scopes by
    with caller_standing(standing, standing):
        async with acting_as(user_id):
            source = await source_document(document_id)
            chunks = source.chunks
            facts = await source_live_facts(chunks)
            copies = copied_chunks(chunks, user_id, target)
            session().add(
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
            await session().flush()
            await claim_promoted_entities(facts, user_id, target)
            await claim_promoted_facts(facts, copies, user_id, target)
    promoted = 1 + len(chunks) + len(facts)
    logger.info("promoted document {} into {} as {} rows", document_id, to_scopes, promoted)
    return promoted
