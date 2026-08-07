from datetime import UTC, datetime, timedelta

from loguru import logger

from ..background.jobs.maintenance import SystemScheduledJob
from ..config import settings
from ..store import HealthSnapshot
from ..store.engine import Session
from ..store.identity import User
from .probes import health
from .reports import HealthReport


class StoredHealth(HealthReport):
    """One stored health report with how old the reading is."""

    measured_at: datetime
    stale: bool


class HealthSnapshotJob(SystemScheduledJob):
    """Measure the operator health report and leave it where the console can read it.

    The report counts rows and reads policies across every scope, which only the database
    owner may do, and the process serving the console is deliberately denied that credential
    so an internet-facing process never holds it. This runs where the credential legitimately
    lives, so the console reads a measurement rather than taking one.
    """

    async def execute(self) -> None:
        report = await health(include_recall=False)
        async with User.system().owner as session:
            await HealthSnapshot.store(session, report.model_dump(mode="json"))
        logger.info("stored operator health snapshot")


async def stored_health(session: Session) -> StoredHealth | None:
    """Read the last stored report, or nothing until a worker pass has produced one."""
    snapshot = await HealthSnapshot.latest(session)
    if snapshot is None:
        return None
    limit = timedelta(minutes=settings.health_snapshot_stale_minutes)
    measured = snapshot.updated_at
    return StoredHealth(
        **snapshot.report,
        measured_at=measured,
        stale=datetime.now(UTC) - measured > limit,
    )
