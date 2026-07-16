from enum import IntEnum, auto

from patos import sql


class QueueStatus(sql.PGEnum):
    """PgQueuer execution states stored in its native PostgreSQL enum."""

    queued = auto()
    picked = auto()
    successful = auto()
    canceled = auto()
    deleted = auto()
    exception = auto()
    failed = auto()


class JobPriority(IntEnum):
    """Relative priority of background work, where larger values run first."""

    maintenance = 10
    chunk = 50
