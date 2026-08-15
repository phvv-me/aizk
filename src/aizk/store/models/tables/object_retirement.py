from datetime import datetime

import rls
import sqlalchemy as sa
from patos import sql
from patos.sql import Column as C
from patos.sql import NonEmptyString, NonNegativeInt
from sqlalchemy import CheckConstraint, Index

from ....config import settings
from ...mixins import CreatedAt, Id, TableBase


class ObjectRetirement(Id, CreatedAt, TableBase, table=True):
    """Durably defer deletion of one obsolete object-store layout."""

    __table_args__ = (
        CheckConstraint(
            "stored_size >= 0",
            name="ck_object_retirement_stored_size_nonnegative",
        ),
        CheckConstraint("storage_key <> ''", name="ck_object_retirement_storage_key_nonempty"),
        Index("ix_object_retirement_delete_after", "delete_after"),
    )

    storage_key = sql.Field(NonEmptyString, max_length=512, unique=True)
    storage_version = sql.Field(str | None, default=None, max_length=512)
    stored_size: C[NonNegativeInt]
    delete_after: C[datetime]

    @classmethod
    def __rls__(cls) -> tuple[rls.Policy, ...]:
        """Deny every application caller while allowing owner maintenance."""
        return (rls.Policy.select(sa.false(), roles=(settings.app_role,)),)
