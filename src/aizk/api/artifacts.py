from typing import Literal

from patos import FrozenModel

from ..config import settings
from ..store import Artifact
from ..store.identity import User
from ..store.models.tables import ArtifactContent
from .dashboard import ScopedRow

type ArtifactStatus = Literal["queued", "processing", "ready", "failed"]


class ArtifactView(ScopedRow):
    """Human-facing progress for one visible original without storage internals."""

    name: str
    status: ArtifactStatus
    detail: str

    @classmethod
    def from_row(
        cls,
        artifact: Artifact,
        content: ArtifactContent,
        user: User,
    ) -> ArtifactView:
        """Present one RLS-visible original using stable user-facing workflow states."""
        status, detail = cls.describe(content.state)
        scopes = set(user.scope_labels(content.scopes))
        return cls(
            name=artifact.name,
            source_uri=artifact.source_uri or "",
            status=status,
            detail=detail,
            date=cls.format_date(content.created_at),
            scopes=tuple(sorted(scopes, key=lambda scope: (scope != "Private", scope.casefold()))),
        )

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
        return cls(
            artifacts=tuple(
                ArtifactView.from_row(artifact, content, user) for artifact, content in rows
            )
        )
