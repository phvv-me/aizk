import abc
from datetime import datetime
from typing import ClassVar

import pendulum
from loguru import logger
from patos import FrozenModel

from ..background.jobs.maintenance import SystemScheduledJob
from ..config import settings
from ..store import OperatorReading, OperatorSnapshot
from ..store.engine import Session
from ..store.identity import User
from .doctor import DoctorReport, doctor
from .probes import health
from .reports import HealthReport
from .usage import PlatformUsage, platform_usage


class Measured(FrozenModel):
    """When a privileged process took a reading, and whether it is too old to show as now."""

    measured_at: datetime
    stale: bool


class StoredHealth(Measured, HealthReport):
    """The stored schema, security, storage, queue, and serving reading."""


class StoredDoctor(Measured, DoctorReport):
    """The stored queue and artifact conversion diagnosis."""


class StoredUsage(Measured, PlatformUsage):
    """The stored platform-wide usage aggregate."""


class SnapshotJob(SystemScheduledJob, abc.ABC):
    """Take one operator reading where the credential lives and leave it for the console.

    Every reading behind this counts rows, reads policies, or aggregates the ledger across
    every scope, which only the database owner may do, and the process serving the console
    is deliberately denied that credential so an internet-facing process never holds it.
    Measuring here is what lets the console read a measurement rather than take one.
    """

    reading: ClassVar[OperatorReading]

    @abc.abstractmethod
    async def measure(self) -> FrozenModel:
        """Take this pass's reading with the platform-wide credential."""

    async def execute(self) -> None:
        report = await self.measure()
        async with User.system().owner as session:
            await OperatorSnapshot.store(session, self.reading, report.model_dump(mode="json"))
        logger.info("stored the operator {} reading", self.reading)


class HealthSnapshotJob(SnapshotJob):
    """Measure schema, row security, storage, queue, and serving health."""

    reading: ClassVar[OperatorReading] = OperatorReading.health

    async def measure(self) -> HealthReport:
        return await health(include_recall=False)


class DoctorSnapshotJob(SnapshotJob):
    """Diagnose queue failures, unhealthy leases, and artifact conversions.

    The stored diagnosis keeps the sanitized error text the console has always shown, since
    an entrypoint and a fingerprint alone rarely tell an operator which failure they are
    looking at. `error_identity` strips every quoted literal before the text is kept, and
    the console only ever serves it behind an operator check.
    """

    reading: ClassVar[OperatorReading] = OperatorReading.doctor

    async def measure(self) -> DoctorReport:
        return await doctor(show_error_messages=True)


class UsageSnapshotJob(SnapshotJob):
    """Aggregate the durable usage ledger across every scope."""

    reading: ClassVar[OperatorReading] = OperatorReading.usage

    async def measure(self) -> PlatformUsage:
        return await platform_usage()


async def stored[T: Measured](
    session: Session, key: OperatorReading, model: type[T], stale_minutes: int
) -> T | None:
    """Read one stored reading, absent until a worker pass has produced it."""
    snapshot = await OperatorSnapshot.latest(session, key)
    if snapshot is None:
        return None
    measured = pendulum.instance(snapshot.updated_at)
    return model.model_validate(
        {
            **snapshot.report,
            "measured_at": measured,
            "stale": measured.diff(pendulum.now("UTC")).in_minutes() > stale_minutes,
        }
    )


async def stored_health(session: Session) -> StoredHealth | None:
    """Read the last health reading a worker pass measured."""
    minutes = settings.health_snapshot_stale_minutes
    return await stored(session, OperatorReading.health, StoredHealth, minutes)


async def stored_doctor(session: Session) -> StoredDoctor | None:
    """Read the last queue and conversion diagnosis a worker pass measured."""
    minutes = settings.doctor_snapshot_stale_minutes
    return await stored(session, OperatorReading.doctor, StoredDoctor, minutes)


async def stored_usage(session: Session) -> StoredUsage | None:
    """Read the last platform-wide usage aggregate a worker pass measured."""
    minutes = settings.usage_snapshot_stale_minutes
    return await stored(session, OperatorReading.usage, StoredUsage, minutes)
