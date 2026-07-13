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
)
from aizk.store.identity import User


async def fresh_owner() -> uuid.UUID:
    await dbutil.reset_db()
    return uuid.uuid4()


async def add_entity(
    session: AsyncSession,
    owner: uuid.UUID,
    name: str,
    type: str = "concept",
    embedding: list[float] | None = None,
    content_id: uuid.UUID | None = None,
) -> uuid.UUID:
    content_id = content_id or uuid.uuid4()
    session.add(EntityContent(id=content_id, name=name, type=type, embedding=embedding))
    await session.flush()
    session.add(EntityClaim(content_id=content_id, created_by=owner, scopes=[owner]))
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
    claim = FactClaim(content_id=content_id, created_by=owner, scopes=[owner], valid=valid)
    if recorded is not None:
        claim.recorded = recorded
    session.add(claim)
    await session.flush()
    return content_id, claim.id


async def seed_chunk(
    owner: uuid.UUID, text: str, title: str | None = None, scopes: tuple[uuid.UUID, ...] = ()
) -> uuid.UUID:
    document, chunk = uuid.uuid7(), uuid.uuid7()
    key = tuple(sorted(set(scopes or (owner,))))
    user = User.authorized(owner, read=key, write=key)
    async with user as session:
        session.add(
            Document(
                id=document,
                content_hash=uuid.uuid4().hex,
                created_by=owner,
                title=title,
                scopes=list(key),
            )
        )
        session.add(
            Chunk(
                id=chunk,
                document_id=document,
                ord=0,
                text=text,
                created_by=owner,
                scopes=list(key),
            )
        )
    return chunk


async def seed_scoped_row(owner: uuid.UUID, kind: str) -> None:
    async with User.private(owner) as session:
        if kind == "community":
            session.add(
                Community(
                    created_by=owner,
                    scopes=[owner],
                    label="theme",
                    summary="a summary",
                    embedding=None,
                )
            )
        elif kind == "profile":
            subject = await add_entity(session, owner, "Ada", type="author")
            session.add(
                Profile(
                    created_by=owner,
                    scopes=[owner],
                    subject_id=subject,
                    summary="a portrait",
                )
            )
        else:
            document = uuid.uuid4()
            session.add(
                Document(
                    id=document,
                    content_hash="c",
                    created_by=owner,
                    scopes=[owner],
                    title="doc",
                )
            )
            session.add(
                Chunk(
                    document_id=document,
                    ord=0,
                    text="a span",
                    created_by=owner,
                    scopes=[owner],
                )
            )
