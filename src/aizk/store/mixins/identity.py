import uuid
from datetime import datetime
from typing import cast

from patos import sql
from pydantic import UUID5, UUID7
from sqlalchemy import DateTime, func
from sqlmodel import Field


class Id:
    """Client-generated, time-ordered UUID primary key."""

    id: sql.Column[UUID7] = Field(default_factory=uuid.uuid7, primary_key=True)


class DeterministicId:
    """Deterministic UUID5 primary key derived from canonical content."""

    id: sql.Column[UUID5] = Field(primary_key=True)


class Timestamped:
    """Server-stamped creation and last-update times."""

    created_at: sql.Column[datetime] = Field(
        default=None,
        nullable=False,
        sa_type=cast(type[datetime], DateTime(timezone=True)),
        sa_column_kwargs={"server_default": func.now()},
    )
    updated_at: sql.Column[datetime] = Field(
        default=None,
        nullable=False,
        sa_type=cast(type[datetime], DateTime(timezone=True)),
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()},
    )
