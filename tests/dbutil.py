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
from aizk.store.engine import _current_session, caller_standing


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
# There are no user, group, or membership tables any more: identity is derived from the token, so
# a test names an owner or an org by a bare uuid (or `store.identity`'s `user_uuid`/`org_uuid`) and
# grants a caller its standing through `caller_standing` rather than seeding a membership row.
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
    lens: tuple[uuid.UUID, ...] = (),
    orgs: tuple[uuid.UUID, ...] = (),
) -> set[uuid.UUID]:
    """The candidate document ids the user reads under RLS, given its org standing and read lens.

    `orgs` is the caller's org membership the read policy admits shared rows against, `lens` the
    optional narrowing to one scope combination's composed graph, the two knobs the token supplies
    in production here supplied by the test directly.
    """
    with caller_standing(orgs, ()):
        async with acting_as(user_id, lens) as session:
            rows = await session.execute(
                text("SELECT id FROM document WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": [str(c) for c in candidates]},
            )
            return set(rows.scalars().all())


async def can_read_document(
    user_id: uuid.UUID,
    doc_id: uuid.UUID,
    lens: tuple[uuid.UUID, ...] = (),
    orgs: tuple[uuid.UUID, ...] = (),
) -> bool:
    """Whether the user can read one document under RLS, given its org standing and read lens."""
    return doc_id in await visible_document_ids(user_id, [doc_id], lens, orgs)


async def can_write_document(
    user_id: uuid.UUID,
    owner_id: uuid.UUID,
    scopes: Sequence[uuid.UUID],
    writable_orgs: tuple[uuid.UUID, ...] = (),
) -> bool:
    """Whether the user may insert a document with this owner and scope set under RLS.

    Attempts the real INSERT under the caller's writable standing, returning False when the
    write-check policy raises Postgres's row-level-security violation and True when the row lands,
    so the test reads the DB's own enforcement of `ScopeLattice.write` rather than a
    reimplementation of it. `writable_orgs` is the editor-or-admin standing the token would carry.
    """
    try:
        with caller_standing(writable_orgs, writable_orgs):
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
