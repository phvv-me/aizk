import asyncio
import functools
import uuid
from collections.abc import AsyncGenerator, Coroutine, Sequence
from contextlib import asynccontextmanager
from typing import cast

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from aizk.config import settings
from aizk.store import acting_as
from aizk.store.engine import _current_session


@asynccontextmanager
async def use_session(fake: object) -> AsyncGenerator[AsyncSession]:
    """Bind a fake session to the task-local context so `session()` resolves to it in a unit test.

    Production `acting_as`/`bypass_rls` bind their real session inline; a faking test binds its
    stand-in the same way here, reaching the store's context var directly.
    """
    bound = cast(AsyncSession, fake)
    token = _current_session.set(bound)
    try:
        yield bound
    finally:
        _current_session.reset(token)


# every app-owned table, ordered so a single TRUNCATE ... CASCADE wipes the world between DB
# examples. `live_fact` is a view over `fact_claim`/`fact_content`, so it is never truncated.
APP_TABLES = (
    "membership",
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
    "group_",
    "users",
)


def run[T](coro: Coroutine[object, object, T]) -> T:
    """Drive one coroutine to completion on a fresh event loop, the sync-test bridge.

    The suite avoids a pytest-asyncio dependency and runs each async scenario through its own
    `asyncio.run`, exactly the per-test loop the NullPool engine is configured for.
    """
    return asyncio.run(coro)


@functools.cache
def admin_engine() -> AsyncEngine:
    """The owner-role (superuser) engine that bypasses row level security, for seeding and cleanup.

    Seeding arbitrary `(owner, scopes)` rows and tearing the world down must reach past the very
    policies the app-role engine enforces, so this connects as the migration owner, which bypasses
    RLS, rather than fighting the write policies to place a probe row.
    """
    return create_async_engine(
        settings.admin_database_url, poolclass=__import__("sqlalchemy").NullPool
    )


async def admin_exec(sql: str, params: dict[str, object] | None = None) -> None:
    """Run one owner-role statement outside row level security."""
    async with admin_engine().begin() as connection:
        await connection.execute(text(sql), params or {})


async def reset_db() -> None:
    """Truncate every app table so each DB example starts from an empty, isolated schema."""
    await admin_exec(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE")


async def seed_user(user_id: uuid.UUID, is_admin: bool = False) -> uuid.UUID:
    """Insert one user, optionally carrying the server-wide admin flag."""
    await admin_exec(
        "INSERT INTO users (id, is_admin) VALUES (:id, :is_admin)",
        {"id": user_id, "is_admin": is_admin},
    )
    return user_id


async def seed_group(
    group_id: uuid.UUID,
    name: str | None = None,
    public: bool = False,
    curated: bool = False,
) -> uuid.UUID:
    """Insert one group with its visibility and curation flags."""
    await admin_exec(
        "INSERT INTO group_ (id, name, public, curated) VALUES (:id, :name, :public, :curated)",
        {"id": group_id, "name": name or f"g-{group_id}", "public": public, "curated": curated},
    )
    return group_id


async def seed_membership(user_id: uuid.UUID, group_id: uuid.UUID, role: str) -> None:
    """Insert one membership row binding a user to a group in a role."""
    await admin_exec(
        "INSERT INTO membership (user_id, group_id, role) "
        "VALUES (:p, :g, CAST(:role AS membership_role))",
        {"p": user_id, "g": group_id, "role": role},
    )


async def seed_document(
    owner_id: uuid.UUID, scopes: Sequence[uuid.UUID], doc_id: uuid.UUID | None = None
) -> uuid.UUID:
    """Insert one document with an arbitrary owner and scope set, bypassing the write policy."""
    doc_id = doc_id or uuid.uuid4()
    await admin_exec(
        "INSERT INTO document (id, kind, content_hash, owner_id, scopes) "
        "VALUES (:id, 'note', 'seed', :owner, CAST(:scopes AS uuid[]))",
        {"id": doc_id, "owner": owner_id, "scopes": [str(s) for s in scopes]},
    )
    return doc_id


async def visible_document_ids(
    user_id: uuid.UUID,
    candidates: Sequence[uuid.UUID],
    scopes: tuple[uuid.UUID, ...] = (),
) -> set[uuid.UUID]:
    """The candidate document ids the user reads under RLS, narrowed by the optional lens."""
    async with acting_as(user_id, scopes) as session:
        rows = await session.execute(
            text("SELECT id FROM document WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [str(c) for c in candidates]},
        )
        return set(rows.scalars().all())


async def can_read_document(
    user_id: uuid.UUID, doc_id: uuid.UUID, scopes: tuple[uuid.UUID, ...] = ()
) -> bool:
    """Whether the user can read one document under RLS, narrowed by the optional lens."""
    return doc_id in await visible_document_ids(user_id, [doc_id], scopes)


async def can_write_document(
    user_id: uuid.UUID, owner_id: uuid.UUID, scopes: Sequence[uuid.UUID]
) -> bool:
    """Whether the user may insert a document with this owner and scope set under RLS.

    Attempts the real INSERT under `acting_as`, returning False when the write-check policy raises
    Postgres's row-level-security violation and True when the row lands, so the test reads the DB's
    own enforcement of `ScopeLattice.write` rather than a reimplementation of it.
    """
    try:
        async with acting_as(user_id) as session:
            await session.execute(
                text(
                    "INSERT INTO document (id, kind, content_hash, owner_id, scopes) "
                    "VALUES (:id, 'note', 'w', :owner, CAST(:scopes AS uuid[]))"
                ),
                {"id": uuid.uuid4(), "owner": owner_id, "scopes": [str(s) for s in scopes]},
            )
    except DBAPIError as error:
        if "row-level security" in str(error).lower() or "violates" in str(error).lower():
            return False
        raise
    return True
