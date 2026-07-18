from datetime import datetime
from enum import auto

import rls
import sqlalchemy as sa
from patos import sql
from patos.sql import Column as C
from patos.sql import NonEmptyString, NonNegativeInt
from pydantic import UUID8
from sqlalchemy import CheckConstraint, Index, Uuid
from sqlmodel import select

from ....config import settings
from ...mixins import CreatedAt, Id, TableBase


class Blob(Id, CreatedAt, TableBase, table=True):
    """Immutable object metadata plus mutable integrity observations.

    PostgreSQL stores integrity and location metadata only. The referenced object
    store owns the potentially large bytes. Reads are authorized through visible
    `Artifact.Content` rows so an unreferenced or foreign blob is not discoverable.
    """

    class Encoding(sql.PGEnum):
        """Lossless representation used only inside object storage."""

        identity = auto()
        zstd = auto()

    __table_args__ = (
        CheckConstraint("size >= 0", name="ck_blob_size_nonnegative"),
        CheckConstraint("stored_size >= 0", name="ck_blob_stored_size_nonnegative"),
        CheckConstraint("stored_size <= size", name="ck_blob_stored_size_bounded"),
        CheckConstraint("storage_key <> ''", name="ck_blob_storage_key_nonempty"),
        Index("ix_blob_content_hash_size", "content_hash", "size"),
        Index("ix_blob_integrity_checked_at", "integrity_checked_at"),
    )

    content_hash: C[UUID8]
    size: C[NonNegativeInt]
    stored_size: C[NonNegativeInt]
    encoding = sql.Field(
        Encoding,
        default=Encoding.identity,
    )
    storage_key = sql.Field(
        NonEmptyString,
        max_length=512,
        unique=True,
    )
    storage_version = sql.Field(
        str | None,
        default=None,
        max_length=512,
    )
    media_type = sql.Field(
        str | None,
        default=None,
        max_length=255,
    )
    etag = sql.Field(
        str | None,
        default=None,
        max_length=512,
    )
    integrity_checked_at = sql.Nullable(datetime)
    integrity_error = sql.Field(
        str | None,
        default=None,
        max_length=1024,
    )

    @classmethod
    def __rls__(cls) -> tuple[rls.Policy, ...]:
        """Permit inserts while exposing metadata only through visible artifacts."""
        content = sa.table(
            "artifact_content",
            sa.column("blob_id", Uuid()),
        )
        return (
            rls.Policy.select(
                "blob_read",
                cls.id.in_(select(content.c.blob_id)),
                roles=(settings.app_role,),
            ),
            rls.Policy.insert("blob_insert", sa.true(), roles=(settings.app_role,)),
        )
