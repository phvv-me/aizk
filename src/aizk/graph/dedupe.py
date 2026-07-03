from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..store.mixins import TableBase


async def mint_content(session: AsyncSession, content: TableBase) -> None:
    """Insert one content row, tolerating a duplicate another tenant already minted.

    Content is visible only through a claim (`store.rls.predicates.content_policies`), so Postgres
    refuses to even plan a plain `INSERT ... ON CONFLICT` against it: row level security requires
    the acting role to hold unconditional SELECT visibility into whatever row might conflict
    before it will plan an `ON CONFLICT` clause at all, DO NOTHING included, precisely so a hidden
    conflict can never resolve silently, the very existence side channel this content/claim split
    exists to close from the other direction. A savepoint is the idiom that stays compatible with
    that Postgres restriction: attempt the insert inside a nested transaction, and a
    `UniqueViolation` on the content's own content-addressed primary key rolls back only the
    savepoint, so the surrounding write proceeds identically whether this exact content already
    existed or not, with no crash and no observable difference between the two cases either way.

    session: open, principal-scoped session the insert runs on.
    content: the transient content row to insert, its primary key already content-addressed.
    """
    try:
        async with session.begin_nested():
            session.add(content)
            await session.flush()
    except IntegrityError:
        pass
