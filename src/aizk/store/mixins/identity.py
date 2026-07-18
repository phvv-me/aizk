from datetime import UTC, datetime

from patos import sql
from pydantic import UUID5, UUID7
from sqlalchemy import func


class Id(sql.Model):
    """Client-generated, time-ordered UUID primary key."""

    id = sql.PK(UUID7)


class DeterministicId(sql.Model):
    """Deterministic UUID5 primary key derived from canonical content."""

    id = sql.PK(UUID5)


class CreatedAt(sql.Model):
    """Creation time stamped by both Python and PostgreSQL."""

    created_at = sql.Field(
        datetime,
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )


class UpdatedAt(sql.Model):
    """Last-update time refreshed by SQLAlchemy on every changed row."""

    updated_at = sql.Field(
        datetime,
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        onupdate=func.now(),
    )


class Timestamped(CreatedAt, UpdatedAt):
    """Creation and last-update times for mutable records."""
