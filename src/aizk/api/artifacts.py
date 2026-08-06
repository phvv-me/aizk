from typing import Literal

from patos import FrozenModel

from ..config import settings
from ..store import Artifact
from ..store.identity import User
from ..store.models.tables import ArtifactContent
from ..store.models.tables.artifact import RecentArtifact
from .dashboard import ScopedRow

type ArtifactStatus = Literal["queued", "processing", "ready", "failed"]


class ArtifactView(ScopedRow):
    """Human-facing progress for one visible original without storage internals."""

    name: str
    status: ArtifactStatus
    detail: str

    @classmethod
    def from_row(cls, row: RecentArtifact, user: User) -> ArtifactView:
        """Present one RLS-visible original using stable user-facing workflow states."""
        name, source_uri, state, scopes, created_at = row
        status, detail = cls.describe(state)
        labels = set(user.scope_labels(scopes))
        return cls(
            name=name,
            source_uri=source_uri or "",
            status=status,
            detail=detail,
            date=cls.format_date(created_at),
            scopes=tuple(sorted(labels, key=cls.scope_order)),
        )

    @staticmethod
    def scope_order(label: str) -> tuple[bool, str]:
        """Order scope labels with Private first, then case-insensitively."""
        return (label != "Private", label.casefold())

    @staticmethod
    def describe(state: ArtifactContent.State) -> tuple[ArtifactStatus, str]:
        """Translate durable processing state into concise, non-internal feedback."""
        match state:
            case ArtifactContent.State.pending | ArtifactContent.State.queued:
                return "queued", "Waiting for secure document processing."
            case ArtifactContent.State.processing:
                return "processing", "Converting and indexing this source."
            case ArtifactContent.State.ready:
                return "ready", "Available to recall."
            case ArtifactContent.State.failed:
                return "failed", "Processing failed. You can try this source again."
        raise ValueError(f"unsupported artifact state {state!r}")


class ArtifactDashboard(FrozenModel):
    """Recent artifact processing loaded entirely through caller-bound RLS reads."""

    artifacts: tuple[ArtifactView, ...] = ()

    @classmethod
    async def load(
        cls,
        user: User,
        limit: int = settings.web_recent_artifact_limit,
    ) -> ArtifactDashboard:
        """Load recent originals visible to the current caller and no storage metadata."""
        async with user as session:
            rows = (await session.exec(Artifact.recent(limit))).all()
        return cls(artifacts=tuple(ArtifactView.from_row(row, user) for row in rows))
