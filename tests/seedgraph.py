import uuid
from datetime import datetime

import dbutil
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from aizk.store import (
    Chunk,
    Community,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    Profile,
    acting_as,
)


async def fresh_owner(is_admin: bool = False) -> uuid.UUID:
    """Wipe every app table and seed one principal, the isolated start of a graph DB test.

    is_admin: whether the seeded principal carries the server-wide admin flag.
    """
    await dbutil.reset_db()
    return await dbutil.seed_user(uuid.uuid4(), is_admin=is_admin)


async def add_entity(
    session: AsyncSession,
    owner: uuid.UUID,
    name: str,
    type: str = "Concept",
    embedding: list[float] | None = None,
    content_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one entity content plus this owner's private claim on it, return the content id.

    session: open session already acting as owner.
    owner: principal that stakes the claim.
    name: entity surface form.
    type: ontology entity type.
    embedding: optional dense vector, null for an unembedded node.
    content_id: pin the content id, or mint a random one.
    """
    content_id = content_id or uuid.uuid4()
    session.add(EntityContent(id=content_id, name=name, type=type, embedding=embedding))
    await session.flush()
    session.add(EntityClaim(content_id=content_id, owner_id=owner))
    await session.flush()
    return content_id


async def add_fact(
    session: AsyncSession,
    owner: uuid.UUID,
    subject_id: uuid.UUID,
    statement: str,
    predicate: str = "related_to",
    object_id: uuid.UUID | None = None,
    embedding: list[float] | None = None,
    valid: Range[datetime] | None = None,
    recorded: Range[datetime] | None = None,
    content_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one fact content plus this owner's claim on it, return its (content id, claim id).

    session: open session already acting as owner.
    owner: principal that stakes the claim.
    subject_id: entity content the fact is about.
    statement: self-contained fact text, the record_access key.
    predicate: closed-vocabulary relation type.
    object_id: entity content the fact points to, null for a unary fact.
    embedding: optional statement vector.
    valid: world-time range, null for undated.
    recorded: transaction-time range, an open server default when null.
    content_id: pin the content id, or mint a random one.
    """
    content_id = content_id or uuid.uuid4()
    session.add(
        FactContent(
            id=content_id,
            subject_id=subject_id,
            object_id=object_id,
            predicate=predicate,
            statement=statement,
            embedding=embedding,
        )
    )
    await session.flush()
    claim = FactClaim(content_id=content_id, owner_id=owner, valid=valid)
    if recorded is not None:
        claim.recorded = recorded
    session.add(claim)
    await session.flush()
    return content_id, claim.id


async def seed_chunk(
    owner: uuid.UUID, text: str, title: str | None = None, scopes: tuple[uuid.UUID, ...] = ()
) -> uuid.UUID:
    """Plant a document and one pending chunk the build extracts a graph slice from, return its id.

    owner: principal that owns the document and chunk.
    text: the span text the chunk carries.
    title: optional document title the source filter matches on.
    scopes: group set the document and chunk are shared with.
    """
    document, chunk = uuid.uuid4(), uuid.uuid4()
    async with acting_as(owner, scopes) as session:
        session.add(
            Document(
                id=document,
                content_hash=uuid.uuid4().hex,
                owner_id=owner,
                title=title,
                scopes=list(scopes),
            )
        )
        session.add(
            Chunk(
                id=chunk,
                document_id=document,
                ord=0,
                text=text,
                owner_id=owner,
                scopes=list(scopes),
            )
        )
    return chunk


async def seed_scoped_row(owner: uuid.UUID, kind: str) -> None:
    """Plant one unembedded scoped row of a chosen table under the owner, for the reembed walk.

    owner: principal that owns the row.
    kind: which table to seed, one of `chunk`, `community`, or `profile`.
    """
    async with acting_as(owner) as session:
        if kind == "community":
            session.add(
                Community(owner_id=owner, label="theme", summary="a summary", embedding=None)
            )
        elif kind == "profile":
            subject = await add_entity(session, owner, "Ada", type="Author")
            session.add(Profile(owner_id=owner, subject_id=subject, summary="a portrait"))
        else:
            document = uuid.uuid4()
            session.add(Document(id=document, content_hash="c", owner_id=owner, title="doc"))
            session.add(Chunk(document_id=document, ord=0, text="a span", owner_id=owner))
