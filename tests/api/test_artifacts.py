from datetime import UTC, datetime
from typing import cast

import dbutil
import pytest
from factories import seed_artifact
from id_factory import uuid5

from aizk.api.artifacts import ArtifactDashboard, ArtifactView
from aizk.store.identity import OrganizationStanding, User
from aizk.store.models.tables import ArtifactContent

pytestmark = pytest.mark.usefixtures("migrated_db")


@pytest.mark.parametrize(
    ("state", "status", "detail"),
    [
        (ArtifactContent.State.pending, "queued", "Waiting"),
        (ArtifactContent.State.queued, "queued", "Waiting"),
        (ArtifactContent.State.processing, "processing", "Converting"),
        (ArtifactContent.State.ready, "ready", "Available"),
        (ArtifactContent.State.failed, "failed", "Processing failed"),
    ],
)
def test_artifact_view_describes_each_human_workflow_state(
    state: ArtifactContent.State,
    status: str,
    detail: str,
) -> None:
    actual_status, actual_detail = ArtifactView.describe(state)

    assert actual_status == status
    assert actual_detail.startswith(detail)


def test_artifact_view_rejects_an_unknown_programming_state() -> None:
    with pytest.raises(ValueError, match="unsupported artifact state"):
        ArtifactView.describe(cast(ArtifactContent.State, "unknown"))


def test_artifact_dashboard_reads_only_visible_originals_through_rls() -> None:
    async def load() -> ArtifactDashboard:
        await dbutil.reset_db()
        caller, organization, outsider = uuid5(), uuid5(), uuid5()
        await seed_artifact(
            caller,
            [caller, organization],
            name="paper.pdf",
            state=ArtifactContent.State.processing,
            created_at=datetime(2026, 7, 16, tzinfo=UTC),
            source_uri="https://papers.test/paper.pdf",
        )
        await seed_artifact(
            caller,
            [caller],
            name="notes.txt",
            state=ArtifactContent.State.ready,
            created_at=datetime(2026, 7, 17, tzinfo=UTC),
        )
        await seed_artifact(
            outsider,
            [outsider],
            name="hidden.pdf",
            state=ArtifactContent.State.failed,
            created_at=datetime(2026, 7, 18, tzinfo=UTC),
        )
        user = User.authorized(
            caller,
            read=(caller, organization),
            organizations=(OrganizationStanding(id=organization, name="Robotics Lab"),),
        )
        return await ArtifactDashboard.load(user, limit=2)

    dashboard = dbutil.run(load())

    assert [artifact.name for artifact in dashboard.artifacts] == ["notes.txt", "paper.pdf"]
    assert dashboard.artifacts[0].status == "ready"
    assert dashboard.artifacts[0].date == "Jul 17, 2026"
    assert dashboard.artifacts[0].scopes == ("Private",)
    assert dashboard.artifacts[1].source_uri == "https://papers.test/paper.pdf"
    assert dashboard.artifacts[1].scopes == ("Private", "Robotics Lab")
