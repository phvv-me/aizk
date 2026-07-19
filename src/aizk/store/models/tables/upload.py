from datetime import datetime
from typing import ClassVar

from patos import sql
from patos.sql import NonEmptyString
from sqlalchemy import Index

from ...mixins import CreatedAt, Scoped, TableBase


class UploadCapability(CreatedAt, Scoped, TableBase, table=True):
    """One live single-use upload capability redeemable by any AIZK process.

    Rows live under the system scope, so only system sessions mint, count, and
    claim them, while the capability string stays the only secret a client holds.
    The `ticket` payload restores the verified caller and declared file when the
    API service redeems a grant minted by the separate MCP server process.
    """

    mutable: ClassVar[bool] = False
    deletable: ClassVar[bool] = True

    __table_args__ = (Index("ix_upload_capability_scopes", "scopes", postgresql_using="gin"),)

    capability = sql.Field(
        NonEmptyString,
        max_length=128,
        primary_key=True,
    )
    ticket = sql.Field(dict, sa_type=sql.TypedJSONB)
    expires_at = sql.Field(datetime, index=True)
