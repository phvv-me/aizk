from sqlalchemy import delete, or_
from sqlmodel import select

from aizk.config import settings
from aizk.store import (
    Chunk,
    Community,
    Document,
    Entity,
    Fact,
    Profile,
    SessionItem,
    Watermark,
)
from aizk.store.identity import User
from aizk.types import Scopes


async def purge_scope(scopes: Scopes) -> None:
    """Delete one evaluation scope and content left unclaimed afterward."""
    key = sorted(scopes)
    async with User.system().owner as opened:
        for model in (
            Fact.Claim,
            Community,
            Profile,
            SessionItem,
            Entity.Claim,
            Chunk,
            Document,
            Watermark,
        ):
            statement = delete(model).where(model.scopes == key)
            if model is Fact.Claim:
                statement = statement.execution_options(**{settings.skip_live_gate: True})
            await opened.exec(statement)

        claimed_fact = (
            select(Fact.Claim.id).where(Fact.Claim.content_id == Fact.Content.id).exists()
        )
        await opened.exec(delete(Fact.Content).where(~claimed_fact))

        claimed_entity = (
            select(Entity.Claim.id).where(Entity.Claim.content_id == Entity.Content.id).exists()
        )
        referenced_entity = (
            select(Fact.Content.id)
            .where(
                or_(
                    Fact.Content.subject_id == Entity.Content.id,
                    Fact.Content.object_id == Entity.Content.id,
                )
            )
            .exists()
        )
        await opened.exec(delete(Entity.Content).where(~claimed_entity, ~referenced_entity))
