from collections.abc import Sequence
from typing import ClassVar, Self

import rls
import sqlalchemy as sa
from patos import Model
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from ...config import settings
from ..engine import Session


class ClaimedContent(Model):
    """Canonical content visible through any readable scoped claim."""

    __table__: ClassVar[Table]
    claim_table: ClassVar[Table]

    async def mint(self, session: Session) -> bool:
        """Insert this content once while containing an expected uniqueness race."""
        try:
            async with session.begin_nested():
                await session.exec(insert(self.__table__).values(self.model_dump()))
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) != "23505":
                raise
            return False
        return True

    @classmethod
    async def mint_all(cls, session: Session, contents: Sequence[Self]) -> None:
        """Insert a content batch once, isolating only the rare deterministic ID race."""
        if not contents:
            return
        try:
            async with session.begin_nested():
                await session.exec(
                    insert(cls.__table__).values([content.model_dump() for content in contents])
                )
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) != "23505":
                raise
            for content in contents:
                await content.mint(session)

    @classmethod
    def __rls__(cls) -> tuple[rls.Policy, ...]:
        content = cls.__table__.c
        claims = cls.claim_table.c
        return (
            rls.Policy.select(
                "content_read",
                content.id.in_(sa.select(claims.content_id)),
                roles=(settings.app_role,),
            ),
            rls.Policy.insert("content_insert", sa.true(), roles=(settings.app_role,)),
        )
