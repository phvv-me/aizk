import uuid

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload

from ..config import settings
from ..exceptions import NotVisibleError
from ..store import (
    Chunk,
    Document,
    EntityClaim,
    FactClaim,
    Group,
    LiveFact,
    Membership,
    acting_as,
)

# the partial live-uniqueness `fact_claim` carries, the same arbiter `graph.build.GraphWriter` ON
# CONFLICT DO NOTHING targets, so re-promoting a document a second time lands the identical claim
# rather than a duplicate live one or a unique-violation crash.
FACT_CLAIM_LIVE_ARBITER = {
    "index_elements": ["content_id", "owner_id", "scope"],
    "index_where": text("upper_inf(recorded)"),
}


async def promote(
    document_id: uuid.UUID,
    to_scope: str,
    principal_id: uuid.UUID | None = None,
) -> int:
    """Copy a document and its chunks into a wider scope, and claim its facts there too.

    References flow one way up the lattice, private to team to org and never down: promotion reads
    the source under the promoter's visibility and writes a fresh copy owned by the promoter into
    the target group, never mutating the source. The document copy carries promoted_from pointing
    back to the original for provenance; its chunks are copied wholesale, but its facts are never
    copied, since fact content is already global and deduplicated — promotion instead claims that
    same content in the target scope, each new claim's own promoted_from pointing back at the
    source claim it was promoted from. The subject and object entity content each promoted fact
    touches also earns the promoter's own claim in the target scope, so a target-scope member reads
    the entity's name and type directly rather than only reaching its id through the fact. A
    curated target reviews the claimed facts immediately only when the promoter already holds its
    admin role, otherwise they land pending like any other curated write.

    document_id: source document to promote, read under the promoter's visibility.
    to_scope: name of the target group the copy is published into, one step wider than the source.
    principal_id: the promoter, owner of the new copy and the principal the writes act as, the
        system principal when null.
    """
    principal_id = principal_id or settings.system_principal_id
    async with acting_as(principal_id) as session:
        group = await Group.named(session, to_scope)
        target = group.id
        # the write policies already refuse a copy into a scope the promoter holds no writer role
        # in, but checking here first fails fast with a clear error before any row is written.
        writable = await session.scalar(
            Membership.writable_group_ids(principal_id).where(Membership.group_id == target)
        )
        if writable is None:
            raise ValueError(f"principal {principal_id} may not publish into {to_scope!r}")
        source = await session.get(Document, document_id, options=[selectinload(Document.chunks)])
        if source is None:
            raise NotVisibleError(f"no visible document {document_id}")
        # resolved once for the whole copy, since every promoted claim shares the same target
        # scope and promoter; a curated target stamps immediately only when the promoter already
        # holds its admin role, otherwise the copies land pending like any other curated write.
        stamp = await Group.review_stamp(session, target, principal_id)
        # the relationship's own order_by="Chunk.ord" keeps this list in document order
        chunks = source.chunks
        # only the live facts travel, since promotion publishes current knowledge, not superseded
        # history, and a source_chunk_id in this set excludes any claim whose chunk is gone.
        # Reading `LiveFact` also skips the `do_orm_execute` listener's `FactClaim`-keyed curation
        # gate (a distinct mapped class never picks it up), so a still-pending source claim travels
        # too, re-stamped fresh for the target scope below like any other curated write.
        facts = list(
            await session.scalars(
                select(LiveFact).where(
                    LiveFact.source_chunk_id.in_([chunk.id for chunk in chunks])
                )
            )
        )
        # each source chunk maps to its fresh copy so a promoted claim re-points its provenance at
        # the new chunk rather than the original, keeping the copy a self-contained subgraph.
        copies = {
            chunk.id: Chunk(
                ord=chunk.ord,
                text=chunk.text,
                tokens=chunk.tokens,
                embedding=chunk.embedding,
                owner_id=principal_id,
                scope=target,
            )
            for chunk in chunks
        }
        session.add(
            Document(
                kind=source.kind,
                title=source.title,
                content_hash=source.content_hash,
                owner_id=principal_id,
                scope=target,
                promoted_from=source.id,
                chunks=list(copies.values()),
            )
        )
        await session.flush()
        entity_ids = {fact.subject_id for fact in facts} | {
            fact.object_id for fact in facts if fact.object_id is not None
        }
        for entity_id in entity_ids:
            await session.execute(
                insert(EntityClaim)
                .values(content_id=entity_id, owner_id=principal_id, scope=target)
                .on_conflict_do_nothing(index_elements=["content_id", "owner_id", "scope"])
            )
        for fact in facts:
            origin = copies[fact.source_chunk_id] if fact.source_chunk_id else None
            if (
                origin is None
            ):  # pragma: no cover - the source_chunk_id IN filter guarantees a copy
                continue
            await session.execute(
                insert(FactClaim)
                .values(
                    content_id=fact.content_id,
                    owner_id=principal_id,
                    scope=target,
                    valid=fact.valid,
                    source_chunk_id=origin.id,
                    attributes=dict(fact.attributes),
                    reviewed_at=stamp,
                    promoted_from=fact.id,
                )
                .on_conflict_do_nothing(**FACT_CLAIM_LIVE_ARBITER)
            )
    promoted = 1 + len(chunks) + len(facts)
    logger.info("promoted document {} into {} as {} rows", document_id, to_scope, promoted)
    return promoted
