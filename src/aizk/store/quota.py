import asyncio
from datetime import UTC, date, datetime
from random import uniform

from loguru import logger
from pydantic import UUID5
from sqlalchemy.exc import DBAPIError

from ..config import Settings, settings
from ..exceptions import QuotaExceededError
from .identity import User
from .models.tables.quota import MonthlyQuotaCounter, QuotaKind
from .models.tables.usage import UsageEvent


class MonthlyQuota:
    """Atomically enforce optional deployment and caller monthly cost limits."""

    def __init__(
        self,
        config: Settings = settings,
        attempts: int = 12,
        backoff_seconds: float = 0.005,
        max_backoff_seconds: float = 0.5,
    ) -> None:
        if attempts < 1:
            raise ValueError("monthly quota requires at least one attempt")
        if backoff_seconds < 0 or max_backoff_seconds < 0:
            raise ValueError("monthly quota backoff cannot be negative")
        self.config = config
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds

    async def consume(self, user_id: UUID5, operation: UsageEvent.Operation) -> None:
        """Consume every applicable allowance in one rollback-safe transaction."""
        limits = self.limits(user_id, operation)
        if not limits:
            return
        period = self.period()
        attempt = 1
        while True:
            try:
                async with User.system().app as session:
                    for subject_id, kind, limit in limits:
                        result = await session.exec(
                            MonthlyQuotaCounter.consume(subject_id, period, kind, limit)
                        )
                        if result.one_or_none() is None:
                            raise QuotaExceededError(f"monthly {kind} limit reached")
                return
            except DBAPIError as error:
                retryable = getattr(error.orig, "sqlstate", None) == "40001"
                if not retryable or attempt == self.attempts:
                    raise
                delay = uniform(
                    0.0,
                    min(self.max_backoff_seconds, self.backoff_seconds * 2 ** (attempt - 1)),
                )
                logger.debug("retrying monthly quota transaction after serialization conflict")
                await asyncio.sleep(delay)
                attempt += 1

    def limits(
        self,
        user_id: UUID5,
        operation: UsageEvent.Operation,
    ) -> tuple[tuple[UUID5, QuotaKind, int], ...]:
        """Return configured global and caller counters for one operation."""
        configured: tuple[tuple[UUID5, QuotaKind, int | None], ...] = (
            (
                self.config.system_user_id,
                "operation",
                self.config.monthly_total_operation_limit,
            ),
            (user_id, "operation", self.config.monthly_user_operation_limit),
        )
        if operation in {
            UsageEvent.Operation.remember_text,
            UsageEvent.Operation.remember_file,
        }:
            configured += (
                (
                    self.config.system_user_id,
                    "remember",
                    self.config.monthly_total_remember_limit,
                ),
                (user_id, "remember", self.config.monthly_user_remember_limit),
            )
        return tuple(
            (subject_id, kind, limit)
            for subject_id, kind, limit in configured
            if limit is not None
        )

    @staticmethod
    def period() -> date:
        """Return the first UTC date of the active accounting month."""
        today = datetime.now(UTC).date()
        return today.replace(day=1)
