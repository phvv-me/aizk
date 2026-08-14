from enum import StrEnum, auto
from typing import ClassVar

import pendulum
import rls
from patos import sql
from pydantic import JsonValue
from sqlalchemy import Text
from sqlmodel import select

from ....config import settings
from ...engine import Session
from ...mixins import Standing, TableBase, Timestamped


class OperatorReading(StrEnum):
    """Which operator measurement one stored row holds."""

    health = auto()
    doctor = auto()
    usage = auto()


class OperatorSnapshot(Timestamped, TableBase, table=True):
    """One operator reading a process holding the owner role last produced.

    The probes behind these readings count rows, read policies, and aggregate the usage
    ledger across every scope, which only the database owner may do, and the public API
    process is deliberately denied that credential so an internet-facing process can never
    hold it. The worker holds it, writes what it measured here, and the console reads these
    rows under the ordinary application role. `updated_at` is what makes a stale reading
    visible as stale rather than passed off as current.
    """

    mutable: ClassVar[bool] = True

    @classmethod
    def __rls__(cls) -> tuple[rls.Policy, ...]:
        """Show these readings only to a caller PostgreSQL can see holds operator standing.

        The readings are platform-wide by construction, so they belong to no scope and the
        ordinary scope policy cannot judge them. They do carry material the rest of the
        schema protects with row security, including the file names of artifacts whose own
        table is scoped, so leaving the table open would mean one forgotten check in one
        endpoint exposes across every tenant what row security exists to keep apart. The
        caller's operator standing already travels into the transaction beside its scopes,
        so the database decides this the same way it decides everything else. An unset
        setting reads as null and denies, which is what makes a caller that never proved
        operator standing see nothing rather than everything.

        There is no write policy. Every pass is written by the worker under the owner role,
        which is where the credential to measure these readings legitimately lives.
        """
        return (
            rls.Policy.select(
                Standing.operator(),
                roles=(settings.app_role,),
            ),
        )

    key = sql.Field(OperatorReading, primary_key=True, sa_type=Text)
    report = sql.Field(dict[str, JsonValue], default_factory=dict, sa_type=sql.TypedJSONB)

    @classmethod
    async def store(
        cls, session: Session, key: OperatorReading, report: dict[str, JsonValue]
    ) -> None:
        """Replace one reading with what this pass measured, stamped with when.

        The time is written rather than left to the ORM's `onupdate`, which fires only when
        a column actually changes. A quiet deployment measures the same report twice, so the
        row would keep the first pass's time forever and age a current reading into a stale
        one while the worker was in fact still running.
        """
        measured = pendulum.now("UTC")
        existing = (await session.exec(select(cls).where(cls.key == key))).one_or_none()
        if existing is None:
            session.add(cls(key=key, report=report, created_at=measured, updated_at=measured))
            return
        existing.report = report
        existing.updated_at = measured

    @classmethod
    async def latest(cls, session: Session, key: OperatorReading) -> OperatorSnapshot | None:
        """Read one stored reading, absent until a worker pass has produced it."""
        return (await session.exec(select(cls).where(cls.key == key))).one_or_none()
