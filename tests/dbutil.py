import asyncio
import functools
from collections.abc import Awaitable, Sequence
from datetime import datetime

from id_factory import uuid7, uuid8
from pydantic import UUID5, UUID7, UUID8
from sqlalchemy import NullPool, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aizk.config import settings
from aizk.store.identity import User

# App-owned tables in dependency-safe truncation order
_APP_TABLES = (
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

_RUNNER = asyncio.Runner()


def actor(user_id: UUID5, scopes: Sequence[UUID5] = ()) -> User:
    """Build a test caller with read and write authority over one exact scope set."""
    authority = tuple(scopes) or (user_id,)
    return User.authorized(user_id, read=authority, write=authority)


def run[T](awaitable: Awaitable[T]) -> T:
    async def resolve() -> T:
        return await awaitable

    return _RUNNER.run(resolve())


def close_runner() -> None:
    """Close the shared event loop used by synchronous tests."""
    _RUNNER.close()


@functools.cache
def admin_engine() -> AsyncEngine:
    return create_async_engine(settings.admin_database_url, poolclass=NullPool)


type SqlValue = str | int | float | bool | datetime | UUID5 | UUID7 | UUID8 | list[str] | None


async def admin_exec(sql: str, params: dict[str, SqlValue] | None = None) -> None:
    async with admin_engine().begin() as connection:
        await connection.execute(text(sql), params or {})


async def reset_db() -> None:
    await admin_exec(f"TRUNCATE {', '.join(_APP_TABLES)} RESTART IDENTITY CASCADE")


async def seed_document(
    created_by: UUID5, scopes: Sequence[UUID5], doc_id: UUID7 | None = None
) -> UUID7:
    doc_id = doc_id or uuid7()
    await admin_exec(
        "INSERT INTO document (id, content_hash, created_by, scopes) "
        "VALUES (:id, :hash, :owner, CAST(:scopes AS uuid[]))",
        {
            "id": doc_id,
            "hash": uuid8(),
            "owner": created_by,
            "scopes": [str(s) for s in scopes],
        },
    )
    return doc_id


async def visible_document_ids(
    user_id: UUID5,
    candidates: Sequence[UUID7],
    orgs: tuple[UUID5, ...] = (),
    public_orgs: tuple[UUID5, ...] = (),
) -> set[UUID7]:
    personal = () if user_id == settings.anonymous_user_id else (user_id,)
    user = User.authorized(user_id, read=(*personal, *orgs), public=public_orgs)
    async with user as session:
        rows = await session.exec(
            text("SELECT id FROM document WHERE id = ANY(CAST(:ids AS uuid[]))"),
            params={"ids": [str(c) for c in candidates]},
        )
        return set(rows.scalars().all())


async def can_read_document(
    user_id: UUID5,
    doc_id: UUID7,
    orgs: tuple[UUID5, ...] = (),
    public_orgs: tuple[UUID5, ...] = (),
) -> bool:
    return doc_id in await visible_document_ids(user_id, [doc_id], orgs, public_orgs)


async def can_write_document(
    user_id: UUID5,
    created_by: UUID5,
    scopes: Sequence[UUID5],
    writable_orgs: tuple[UUID5, ...] = (),
) -> bool:
    try:
        personal = () if user_id == settings.anonymous_user_id else (user_id,)
        authority = (*personal, *writable_orgs)
        user = User.authorized(user_id, read=authority, write=authority)
        async with user as session:
            await session.exec(
                text(
                    "INSERT INTO document (id, content_hash, created_by, scopes) "
                    "VALUES (:id, :hash, :owner, CAST(:scopes AS uuid[]))"
                ),
                params={
                    "id": uuid7(),
                    "hash": uuid8(),
                    "owner": created_by,
                    "scopes": [str(s) for s in scopes],
                },
            )
    except DBAPIError as error:
        if "row-level security" in str(error).lower() or "violates" in str(error).lower():
            return False
        raise
    return True
