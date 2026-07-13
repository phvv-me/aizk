import asyncio
import functools
import uuid
from collections.abc import Coroutine, Sequence

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aizk.config import settings
from aizk.store.identity import User

# App-owned tables in dependency-safe truncation order
APP_TABLES = (
    "document",
    "chunk",
    "entity_claim",
    "entity_content",
    "fact_claim",
    "fact_content",
    "community",
    "profile",
    "session_item",
    "watermark",
)


def actor(user_id: uuid.UUID, scopes: Sequence[uuid.UUID] = ()) -> User:
    """Build a test caller with read and write authority over one exact scope set."""
    authority = tuple(scopes) or (user_id,)
    return User.authorized(user_id, read=authority, write=authority)


def run[T](coro: Coroutine[object, object, T]) -> T:
    return asyncio.run(coro)


@functools.cache
def admin_engine() -> AsyncEngine:
    return create_async_engine(
        settings.admin_database_url, poolclass=__import__("sqlalchemy").NullPool
    )


async def admin_exec(sql: str, params: dict[str, object] | None = None) -> None:
    async with admin_engine().begin() as connection:
        await connection.execute(text(sql), params or {})


async def reset_db() -> None:
    await admin_exec(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE")


async def seed_document(
    created_by: uuid.UUID, scopes: Sequence[uuid.UUID], doc_id: uuid.UUID | None = None
) -> uuid.UUID:
    doc_id = doc_id or uuid.uuid7()
    await admin_exec(
        "INSERT INTO document (id, kind, content_hash, created_by, scopes) "
        "VALUES (:id, 'note', 'seed', :owner, CAST(:scopes AS uuid[]))",
        {"id": doc_id, "owner": created_by, "scopes": [str(s) for s in scopes]},
    )
    return doc_id


async def visible_document_ids(
    user_id: uuid.UUID,
    candidates: Sequence[uuid.UUID],
    orgs: tuple[uuid.UUID, ...] = (),
    public_orgs: tuple[uuid.UUID, ...] = (),
) -> set[uuid.UUID]:
    personal = () if user_id == settings.anonymous_user_id else (user_id,)
    user = User.authorized(user_id, read=(*personal, *orgs), public=public_orgs)
    async with user as session:
        rows = await session.exec(
            text("SELECT id FROM document WHERE id = ANY(CAST(:ids AS uuid[]))"),
            params={"ids": [str(c) for c in candidates]},
        )
        return set(rows.scalars().all())


async def can_read_document(
    user_id: uuid.UUID,
    doc_id: uuid.UUID,
    orgs: tuple[uuid.UUID, ...] = (),
    public_orgs: tuple[uuid.UUID, ...] = (),
) -> bool:
    return doc_id in await visible_document_ids(user_id, [doc_id], orgs, public_orgs)


async def can_write_document(
    user_id: uuid.UUID,
    created_by: uuid.UUID,
    scopes: Sequence[uuid.UUID],
    writable_orgs: tuple[uuid.UUID, ...] = (),
) -> bool:
    try:
        personal = () if user_id == settings.anonymous_user_id else (user_id,)
        authority = (*personal, *writable_orgs)
        user = User.authorized(user_id, read=authority, write=authority)
        async with user as session:
            await session.exec(
                text(
                    "INSERT INTO document (id, kind, content_hash, created_by, scopes) "
                    "VALUES (:id, 'note', 'w', :owner, CAST(:scopes AS uuid[]))"
                ),
                params={
                    "id": uuid.uuid7(),
                    "owner": created_by,
                    "scopes": [str(s) for s in scopes],
                },
            )
    except DBAPIError as error:
        if "row-level security" in str(error).lower() or "violates" in str(error).lower():
            return False
        raise
    return True
