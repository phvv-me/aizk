from sqlalchemy import delete, or_
from sqlmodel import select

from ..config import settings
from ..store import (
    Chunk,
    Community,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    Profile,
    SessionItem,
    Watermark,
)
from ..store.engine import bypass_rls
from ..types import Scopes


async def purge_scope(scopes: Scopes) -> None:
    """Delete one evaluation scope and content left unclaimed afterward."""
    key = sorted(scopes)
    async with bypass_rls() as opened:
        for model in (
            FactClaim,
            Community,
            Profile,
            SessionItem,
            EntityClaim,
            Chunk,
            Document,
            Watermark,
        ):
            statement = delete(model).where(model.scopes == key)
            if model is FactClaim:
                statement = statement.execution_options(**{settings.skip_live_gate: True})
            await opened.exec(statement)

        claimed_fact = select(FactClaim.id).where(FactClaim.content_id == FactContent.id).exists()
        await opened.exec(delete(FactContent).where(~claimed_fact))

        claimed_entity = (
            select(EntityClaim.id).where(EntityClaim.content_id == EntityContent.id).exists()
        )
        referenced_entity = (
            select(FactContent.id)
            .where(
                or_(
                    FactContent.subject_id == EntityContent.id,
                    FactContent.object_id == EntityContent.id,
                )
            )
            .exists()
        )
        await opened.exec(delete(EntityContent).where(~claimed_entity, ~referenced_entity))
