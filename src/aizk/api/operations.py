from asyncio import sleep
from collections.abc import AsyncIterator, Awaitable, Callable
from json import dumps

from ..status import (
    ArtifactProcessingRecord,
    ChunkProcessingRecord,
    ProcessingStatus,
    StageEstimate,
    UsagePoint,
    UsageReport,
    UsageSummary,
)
from ..store.identity import User
from .artifacts import ArtifactDashboard, ArtifactView

__all__ = [
    "ArtifactProcessingRecord",
    "ChunkProcessingRecord",
    "ProcessingReport",
    "ProcessingUpdates",
    "StageEstimate",
    "UsagePoint",
    "UsageReport",
    "UsageSummary",
]

type Disconnected = Callable[[], Awaitable[bool]]
type Pause = Callable[[float], Awaitable[None]]


class ProcessingReport(ProcessingStatus):
    """Browser processing status with recent caller-visible source states."""

    recent: tuple[ArtifactView, ...] = ()

    @classmethod
    async def load(cls, user: User) -> ProcessingReport:
        """Add recent source states to the transport-neutral processing status."""
        report = await ProcessingStatus.load(user)
        recent = await ArtifactDashboard.load(user)
        return cls(
            **report.model_dump(),
            recent=recent.artifacts,
        )


class ProcessingUpdates:
    """Emit caller-visible processing snapshots as a reconnectable event stream."""

    def __init__(
        self,
        user: User,
        disconnected: Disconnected,
        pause: Pause = sleep,
        interval_seconds: float = 5,
    ) -> None:
        self.user = user
        self.disconnected = disconnected
        self.pause = pause
        self.interval_seconds = interval_seconds

    async def events(self) -> AsyncIterator[bytes]:
        """Yield deterministic SSE snapshots until the browser disconnects."""
        event_id = 1
        while not await self.disconnected():
            report = await ProcessingReport.load(self.user)
            payload = dumps(
                report.model_dump(mode="json"),
                separators=(",", ":"),
                sort_keys=True,
            )
            yield (f"id: {event_id}\nevent: processing\nretry: 5000\ndata: {payload}\n\n").encode()
            event_id += 1
            await self.pause(self.interval_seconds)
