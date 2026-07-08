import uuid
from itertools import batched

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..serving import Embedder
from ..store import Chunk, Community, EntityContent, FactContent, Profile, acting_as
from .admin import admin_session

# the three per-tenant embedded tables, each carrying an id, an owner, and a halfvec embedding
# column, re-embedded one user's rows at a time under ordinary row level security.
ScopedEmbedded = Chunk | Community | Profile

# every scoped embedded table paired with the source column its vector is built from, so
# re-embedding re-encodes the stored text in place rather than re-ingesting it.
SCOPED_TARGETS: dict[type[ScopedEmbedded], str] = {
    Chunk: "text",
    Community: "summary",
    Profile: "summary",
}

# the two deduplicated content tables, each carrying its own embedding built from name or
# statement; re-embedded once, system-wide, benefiting every claim regardless of container.
ContentEmbedded = EntityContent | FactContent
CONTENT_TARGETS: dict[type[ContentEmbedded], str] = {
    EntityContent: "name",
    FactContent: "statement",
}

EmbeddedTable = ScopedEmbedded | ContentEmbedded


async def rewrite_embeddings(
    session: AsyncSession, embedder: Embedder, model: type[EmbeddedTable], field: str
) -> int:
    """Re-read one table's source text and overwrite its embedding column in batches.

    An ORM `Session`, not a bare `Connection`, is what makes the batched `update(model)` calls
    below target one row per dict by primary key, a `Session`-level bulk-update feature a plain
    Core connection never applies, which would otherwise set every row in the table to each batch
    entry's values in turn. Returns how many rows were rewritten. The caller owns the session's own
    transaction boundary and visibility (`acting_as` for a per-tenant table, the admin engine for a
    deduplicated content table content's own UPDATE policy refuses under row level security).

    session: open session the read and every batched update run on.
    embedder: backend that maps the source text to fresh vectors.
    model: the embedded ORM table to walk.
    field: name of the source text column the vector is built from.
    """
    column = getattr(model, field)
    rows = (await session.execute(select(model.id, column).order_by(model.id))).all()
    for batch in batched(rows, settings.reembed_batch, strict=False):
        vectors = await embedder.embed([source for _, source in batch], mode="document")
        await session.execute(
            update(model),
            [
                {"id": row_id, "embedding": vector}
                for (row_id, _), vector in zip(batch, vectors, strict=True)
            ],
        )
    return len(rows)


async def reembed_scoped_table(
    embedder: Embedder,
    user_id: uuid.UUID,
    model: type[ScopedEmbedded],
    field: str,
) -> int:
    """Re-embed one per-tenant table's rows under the acting user, return the count rewritten.

    embedder: backend that maps the source text to fresh vectors.
    user_id: identity whose visibility scopes and owns the rewritten rows.
    model: the embedded ORM table to walk.
    field: name of the source text column the vector is built from.
    """
    async with acting_as(user_id) as session:
        count = await rewrite_embeddings(session, embedder, model, field)
    logger.info("re-embedded {} {} rows", count, model.__tablename__)
    return count


async def reembed_content_table(
    embedder: Embedder,
    model: type[ContentEmbedded],
    field: str,
) -> int:
    """Re-embed every row of one deduplicated content table, return how many vectors changed.

    Content carries no owner of its own and no UPDATE policy at all under row level security, so
    an ordinary `acting_as` session can never rewrite its embedding column. This runs instead on
    the owner-role admin engine, the same connection migrations use. A model change benefits every
    claim on the content at once, with no per-user repetition needed.

    embedder: backend that maps the source text to fresh vectors.
    model: the content ORM table to walk.
    field: name of the source text column the vector is built from.
    """
    async with admin_session() as session:
        count = await rewrite_embeddings(session, embedder, model, field)
        await session.commit()
    logger.info("re-embedded {} {} content rows", count, model.__tablename__)
    return count


async def reembed(user_id: uuid.UUID) -> int:
    """Re-encode every stored embedding with the current embedder, the one-command model migration.

    Walks each per-tenant embedded table under the acting user's own row level security, then
    walks the two deduplicated content tables system-wide, re-reading each row's source text and
    overwriting its halfvec with a fresh vector from the configured embedder in batches, so
    switching `embed_model` or `embed_url` needs no re-ingest. The stored text and the schema stay
    untouched, only the embedding columns change.

    user_id: identity whose visibility scopes and owns the rewritten per-tenant rows.
    """
    embedder = Embedder()
    scoped = sum(
        [
            await reembed_scoped_table(embedder, user_id, model, field)
            for model, field in SCOPED_TARGETS.items()
        ]
    )
    content = sum(
        [
            await reembed_content_table(embedder, model, field)
            for model, field in CONTENT_TARGETS.items()
        ]
    )
    return scoped + content
