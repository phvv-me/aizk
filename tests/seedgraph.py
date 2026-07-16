import uuid
from datetime import datetime

import dbutil
from id_factory import uuid5, uuid8
from pydantic import UUID5, UUID7
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from aizk.store import (
    Chunk,
    Community,
    Document,
    Entity,
    Fact,
    Profile,
)
from aizk.store.identity import User


async def fresh_owner() -> UUID5 | UUID7:
    await dbutil.reset_db()
    return uuid5()


async def add_entity(
    session: AsyncSession,
    owner: UUID5 | UUID7,
    name: str,
    type: str = "concept",
    embedding: list[float] | None = None,
    content_id: UUID5 | UUID7 | None = None,
) -> UUID5 | UUID7:
    content_id = content_id or uuid5()
    session.add(Entity.Content(id=content_id, name=name, type=type, embedding=embedding))
    await session.flush()
    session.add(Entity.Claim(content_id=content_id, created_by=owner, scopes=[owner]))
    await session.flush()
    return content_id


async def add_fact(
    session: AsyncSession,
    owner: UUID5 | UUID7,
    subject_id: UUID5 | UUID7,
    statement: str,
    predicate: str = "related_to",
    object_id: UUID5 | UUID7 | None = None,
    embedding: list[float] | None = None,
    valid: Range[datetime] | None = None,
    recorded: Range[datetime] | None = None,
    content_id: UUID5 | UUID7 | None = None,
) -> tuple[UUID5 | UUID7, UUID5 | UUID7]:
    content_id = content_id or uuid5()
    session.add(
        Fact.Content(
            id=content_id,
            subject_id=subject_id,
            object_id=object_id,
            predicate=predicate,
            statement=statement,
            embedding=embedding,
        )
    )
    await session.flush()
    claim = Fact.Claim(content_id=content_id, created_by=owner, scopes=[owner], valid=valid)
    if recorded is not None:
        claim.recorded = recorded
    session.add(claim)
    await session.flush()
    return content_id, claim.id


async def seed_chunk(
    owner: UUID5 | UUID7,
    text: str,
    title: str | None = None,
    scopes: tuple[UUID5 | UUID7, ...] = (),
    subject_type: str | None = None,
) -> UUID5 | UUID7:
    document, chunk = uuid.uuid7(), uuid.uuid7()
    key = tuple(sorted(set(scopes or (owner,))))
    user = User.authorized(owner, read=key, write=key)
    async with user as session:
        session.add(
            Document(
                id=document,
                content_hash=uuid8(),
                created_by=owner,
                title=title,
                subject_type=subject_type,
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


async def seed_scoped_row(owner: UUID5 | UUID7, kind: str) -> None:
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
            document = uuid5()
            session.add(
                Document(
                    id=document,
                    content_hash=uuid8(),
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
