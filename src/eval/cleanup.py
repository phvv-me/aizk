from sqlalchemy import delete, or_, text
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

_DELETE_BATCH = 1


async def purge_scope(scopes: Scopes) -> None:
    """Delete one evaluation scope and content left unclaimed afterward.

    Each delete commits independently so CockroachDB does not build one transaction with
    thousands of trigger-maintained C-SPANN projection writes.
    """
    key = sorted(scopes)
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
        while True:
            selected = select(model.id).where(model.scopes == key).limit(_DELETE_BATCH)
            statement = delete(model).where(model.id.in_(selected)).returning(model.id)
            if model is Fact.Claim:
                statement = statement.execution_options(**{settings.skip_live_gate: True})
            async with User.system().owner as opened:
                connection = await opened.connection()
                await connection.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                removed = (await opened.exec(statement)).first()
            if removed is None:
                break

    claimed_fact = select(Fact.Claim.id).where(Fact.Claim.content_id == Fact.Content.id).exists()
    while True:
        selected = select(Fact.Content.id).where(~claimed_fact).limit(_DELETE_BATCH)
        async with User.system().owner as opened:
            connection = await opened.connection()
            await connection.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            removed = (
                await opened.exec(
                    delete(Fact.Content)
                    .where(Fact.Content.id.in_(selected))
                    .returning(Fact.Content.id)
                )
            ).first()
        if removed is None:
            break

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
    while True:
        selected = (
            select(Entity.Content.id)
            .where(~claimed_entity, ~referenced_entity)
            .limit(_DELETE_BATCH)
        )
        async with User.system().owner as opened:
            connection = await opened.connection()
            await connection.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            removed = (
                await opened.exec(
                    delete(Entity.Content)
                    .where(Entity.Content.id.in_(selected))
                    .returning(Entity.Content.id)
                )
            ).first()
        if removed is None:
            break
