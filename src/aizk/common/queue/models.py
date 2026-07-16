from typing import Self

from patos import FrozenModel
from pgqueuer.domain.settings import DBSettings


class QueuePayload(FrozenModel):
    """Typed JSON payload persisted by PgQueuer."""

    def encode(self) -> bytes:
        """Serialize this payload for PgQueuer storage."""
        return self.model_dump_json().encode()

    @classmethod
    def decode(cls, payload: bytes) -> Self:
        """Validate a PgQueuer payload into its declared type."""
        return cls.model_validate_json(payload)


class QueueSchema(FrozenModel):
    """Names discovered from PgQueuer's own database settings."""

    queue: str
    log: str
    statistics: str
    schedules: str
    status_type: str

    @classmethod
    def from_settings(cls, settings: DBSettings) -> QueueSchema:
        """Capture PgQueuer's configured object names without duplicating its defaults."""
        return cls(
            queue=settings.queue_table,
            log=settings.queue_table_log,
            statistics=settings.statistics_table,
            schedules=settings.schedules_table,
            status_type=settings.queue_status_type,
        )

    @property
    def tables(self) -> tuple[str, ...]:
        """Return every PgQueuer table installed for this namespace."""
        return self.queue, self.log, self.statistics, self.schedules

    @property
    def sequences(self) -> tuple[str, ...]:
        """Return the identity sequence created for each PgQueuer table."""
        return tuple(f"{table}_id_seq" for table in self.tables)
