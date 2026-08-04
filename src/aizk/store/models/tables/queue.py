from datetime import datetime
from typing import ClassVar

import rls
from patos import sql
from pydantic import UUID7
from sqlalchemy import Index, LargeBinary, String, text

from ...mixins import Id, TableBase, Timestamped


class QueueTask(Id, Timestamped, TableBase, table=True):
    """One portable background job with durable retry and deduplication state."""

    mutable: ClassVar[bool] = True
    __rls__ = rls.Open()
    __table_args__ = (
        Index("ix_queue_task_pick", "status", "priority", "created_at"),
        Index(
            "uq_queue_task_active_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text(
                "dedupe_key IS NOT NULL AND status IN ('queued', 'picked', 'failed')"
            ),
        ),
    )

    entrypoint = sql.Field(str, index=True)
    payload = sql.Field(bytes, sa_type=LargeBinary)
    priority = sql.Field(int, default=0)
    dedupe_key = sql.Nullable(str)
    status = sql.Field(str, default="queued", sa_type=String)
    attempts = sql.Field(int, default=0)
    max_attempts = sql.Field(int, default=5)
    available_at = sql.Field(datetime)
    heartbeat_at = sql.Nullable(datetime)
    error_type = sql.Nullable(str)
    error_message = sql.Nullable(str)


class QueueEvent(Id, Timestamped, TableBase, table=True):
    """One immutable-enough execution transition retained for queue diagnosis."""

    mutable: ClassVar[bool] = False
    __rls__ = rls.Open()

    task_id = sql.Field(UUID7, index=True)
    entrypoint = sql.Field(str, index=True)
    status = sql.Field(str, index=True, sa_type=String)
    attempts = sql.Field(int, default=0)
    error_type = sql.Nullable(str)
    error_message = sql.Nullable(str)


class QueueSchedule(Timestamped, TableBase, table=True):
    """One durable cron cursor shared by portable worker replicas."""

    mutable: ClassVar[bool] = True
    __rls__ = rls.Open()

    name = sql.Field(str, primary_key=True)
    expression = sql.Field(str)
    next_run = sql.Field(datetime, index=True)
