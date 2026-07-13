from itertools import batched

from loguru import logger
from sqlalchemy import update
from sqlmodel import select

from ..config import settings
from ..serving import embed
from ..store import Chunk, Community, EntityContent, FactContent, Profile
from ..store.engine import Session, bypass_rls
from ..types import Scopes

ScopedEmbedded = Chunk | Community | Profile

_SCOPED_TARGETS: dict[type[ScopedEmbedded], str] = {
    Chunk: "text",
    Community: "summary",
    Profile: "summary",
}

ContentEmbedded = EntityContent | FactContent
_CONTENT_TARGETS: dict[type[ContentEmbedded], str] = {
    EntityContent: "name",
    FactContent: "statement",
}

EmbeddedTable = ScopedEmbedded | ContentEmbedded


async def rewrite_embeddings(
    session: Session,
    model: type[EmbeddedTable],
    field: str,
    scopes: Scopes | None = None,
) -> int:
    """Re-read one table's source text and overwrite its embedding column in batches."""
    column = getattr(model, field)
    selection = select(model.id, column).order_by(model.id)
    if scopes is not None:
        selection = selection.where(model.__table__.c.scopes == sorted(scopes))
    rows = (await session.exec(selection)).all()
    for batch in batched(rows, settings.reembed_batch, strict=False):
        vectors = await embed([source for _, source in batch], mode="document")
        await session.exec(
            update(model),
            params=[
                {"id": row_id, "embedding": vector}
                for (row_id, _), vector in zip(batch, vectors, strict=True)
            ],
        )
    return len(rows)


async def reembed_scoped_table(
    scopes: Scopes,
    model: type[ScopedEmbedded],
    field: str,
) -> int:
    """Re-embed one per-tenant table's rows under the acting user, return the count
    rewritten."""
    async with bypass_rls() as session:
        count = await rewrite_embeddings(session, model, field, scopes)
    logger.info("re-embedded {} {} rows", count, model.__tablename__)
    return count


async def reembed_content_table(
    model: type[ContentEmbedded],
    field: str,
) -> int:
    """Re-embed every row of one deduplicated content table, return how many vectors changed."""
    async with bypass_rls() as session:
        count = await rewrite_embeddings(session, model, field)
    logger.info("re-embedded {} {} content rows", count, model.__tablename__)
    return count


async def reembed(scopes: Scopes | None = None) -> int:
    """Re-encode every stored embedding with the current embedder, the one-command model
    migration."""
    key = frozenset(scopes or (settings.system_user_id,))
    scoped = sum(
        [await reembed_scoped_table(key, model, field) for model, field in _SCOPED_TARGETS.items()]
    )
    content = sum(
        [await reembed_content_table(model, field) for model, field in _CONTENT_TARGETS.items()]
    )
    return scoped + content
