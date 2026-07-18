from datetime import UTC, datetime

import dbutil
from id_factory import uuid5

from aizk.api.dashboard import Dashboard
from aizk.store.identity import OrganizationStanding, User


def test_dashboard_reads_only_visible_sources_through_user_rls(migrated_db: None) -> None:
    async def load() -> Dashboard:
        await dbutil.reset_db()
        caller, organization, public, stranger = (uuid5() for _ in range(4))
        private = await dbutil.seed_document(caller, [caller])
        shared = await dbutil.seed_document(caller, [caller, organization])
        public_source = await dbutil.seed_document(stranger, [public])
        hidden = await dbutil.seed_document(stranger, [stranger])
        metadata = (
            (
                private,
                "Private note",
                "https://notes.test/private",
                "project",
                datetime(2026, 7, 14, tzinfo=UTC),
                datetime(2026, 7, 15, tzinfo=UTC),
            ),
            (
                shared,
                "Shared paper",
                "https://papers.test/shared",
                "code_artifact",
                None,
                datetime(2026, 7, 16, tzinfo=UTC),
            ),
            (
                public_source,
                None,
                None,
                None,
                None,
                datetime(2026, 7, 17, tzinfo=UTC),
            ),
            (
                hidden,
                "Hidden note",
                None,
                None,
                None,
                datetime(2026, 7, 18, tzinfo=UTC),
            ),
        )
        for document, title, source_uri, subject_type, observed_at, updated_at in metadata:
            await dbutil.admin_exec(
                "UPDATE document SET title = :title, source_uri = :source_uri, "
                "subject_type = :subject_type, observed_at = :observed_at, "
                "updated_at = :updated_at WHERE id = :id",
                {
                    "id": document,
                    "title": title,
                    "source_uri": source_uri,
                    "subject_type": subject_type,
                    "observed_at": observed_at,
                    "updated_at": updated_at,
                },
            )
        user = User.authorized(
            caller,
            read=(caller, organization),
            public=(public,),
            organizations=(OrganizationStanding(id=organization, name="Robotics Lab"),),
        )
        return await Dashboard.load(user)

    dashboard = dbutil.run(load())

    assert dashboard.totals.sources == 3
    assert dashboard.totals.findings == 0
    assert dashboard.totals.subjects == 0
    assert dashboard.totals.themes == 0
    assert [source.title for source in dashboard.recent_sources] == [
        "Untitled source",
        "Shared paper",
        "Private note",
    ]
    assert dashboard.recent_sources[0].kind == "Source"
    assert dashboard.recent_sources[0].scopes == ("Shared",)
    assert dashboard.recent_sources[1].kind == "Code Artifact"
    assert dashboard.recent_sources[1].scopes == ("Private", "Robotics Lab")
    assert dashboard.recent_sources[1].date == "Jul 16, 2026"
    assert dashboard.recent_sources[2].date == "Jul 14, 2026"
