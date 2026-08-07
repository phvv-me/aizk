from typing import ClassVar

import rls
from patos import sql
from pydantic import JsonValue
from sqlmodel import select

from ...engine import Session
from ...mixins import TableBase, Timestamped

_LATEST = "latest"


class HealthSnapshot(Timestamped, TableBase, table=True):
    """The operator health report a process holding the owner role last produced.

    Four of the probes behind that report read the catalog and unfiltered row counts, which
    only the database owner may see, and the public API process is deliberately denied that
    credential so an internet-facing process can never hold it. The worker holds it, writes
    what it measured here, and the console reads this row under the ordinary application
    role. `updated_at` is what makes a stale snapshot visible as stale rather than passed off
    as current.
    """

    __rls__ = rls.Open()

    mutable: ClassVar[bool] = True

    key = sql.Field(str, primary_key=True, default=_LATEST)
    report = sql.Field(dict[str, JsonValue], default_factory=dict, sa_type=sql.TypedJSONB)

    @classmethod
    async def store(cls, session: Session, report: dict[str, JsonValue]) -> None:
        """Replace the single snapshot with what this pass measured."""
        existing = (await session.exec(select(cls).where(cls.key == _LATEST))).one_or_none()
        if existing is None:
            session.add(cls(key=_LATEST, report=report))
            return
        existing.report = report

    @classmethod
    async def latest(cls, session: Session) -> HealthSnapshot | None:
        """Read the stored snapshot, absent until the worker has produced one."""
        return (await session.exec(select(cls).where(cls.key == _LATEST))).one_or_none()
