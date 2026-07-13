import uuid
from datetime import datetime
from typing import cast

from sqlalchemy import DateTime, func
from sqlmodel import Field

from ...common.sql import Column


class Id:
    """Client-generated, time-ordered UUID primary key."""

    id: Column[uuid.UUID] = Field(default_factory=uuid.uuid7, primary_key=True)


class Timestamped:
    """Server-stamped creation and last-update times."""

    created_at: Column[datetime] = Field(
        default=None,
        nullable=False,
        sa_type=cast(type[datetime], DateTime(timezone=True)),
        sa_column_kwargs={"server_default": func.now()},
    )
    updated_at: Column[datetime] = Field(
        default=None,
        nullable=False,
        sa_type=cast(type[datetime], DateTime(timezone=True)),
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()},
    )
