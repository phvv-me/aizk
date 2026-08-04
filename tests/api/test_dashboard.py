from datetime import UTC, datetime

import dbutil
from factories import seed_artifact
from id_factory import uuid5
from pydantic import UUID5
from sqlalchemy import text

from aizk.api.dashboard import Dashboard, KnowledgeTotals
from aizk.store import Community, Entity, Fact, Knowledge
from aizk.store.identity import OrganizationStanding, User


def test_dashboard_reads_only_visible_sources_through_user_rls(migrated_db: None) -> None:
    async def load() -> Dashboard:
        await dbutil.reset_db()
        caller, organization, public, stranger = (uuid5() for _ in range(4))
        private = await dbutil.seed_document(caller, [caller])
        shared = await dbutil.seed_document(caller, [caller, organization])
        public_source = await dbutil.seed_document(stranger, [public])
        hidden = await dbutil.seed_document(stranger, [stranger])
        await seed_artifact(caller, [caller], name="manual.pdf")
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

    # The public source is readable but unwritable, so it stays out of the caller's totals.
    assert dashboard.totals.documents == 2
    assert dashboard.totals.files == 1
    assert dashboard.totals.findings == 0
    assert dashboard.totals.subjects == 0
    assert dashboard.totals.themes == 0
    assert [document.title for document in dashboard.recent_documents] == [
        "Untitled document",
        "Shared paper",
        "Private note",
    ]
    assert dashboard.recent_documents[0].kind == "Source"
    assert dashboard.recent_documents[0].scopes == ("Shared",)
    assert dashboard.recent_documents[1].kind == "Code Artifact"
    assert dashboard.recent_documents[1].scopes == ("Private", "Robotics Lab")
    assert dashboard.recent_documents[1].date == "Jul 16, 2026"
    assert dashboard.recent_documents[2].date == "Jul 14, 2026"


async def seed_corpus(owner: UUID5, scopes: list[UUID5]) -> None:
    """Write one document, file, subject, finding, and theme into one scope set."""
    await dbutil.seed_document(owner, scopes)
    await seed_artifact(owner, scopes, name="handbook.pdf")
    subject, object_ = uuid5(), uuid5()
    async with dbutil.actor(owner, scopes) as session:
        session.add_all(
            Entity.Content(id=entity, name=f"entity {entity}", type="concept")
            for entity in (subject, object_)
        )
        fact = Fact.Content(
            id=uuid5(),
            subject_id=subject,
            object_id=object_,
            predicate="related_to",
            statement="one links the other",
        )
        session.add(fact)
        await session.flush()
        session.add_all(
            (
                Entity.Claim(content_id=subject, created_by=owner, scopes=scopes),
                Fact.Claim(content_id=fact.id, created_by=owner, scopes=scopes),
                Community(
                    created_by=owner,
                    scopes=scopes,
                    label="A theme",
                    summary="What the theme covers",
                    member_ids=[subject, object_],
                ),
            )
        )


def test_public_organizations_count_only_where_the_caller_may_also_write(
    migrated_db: None,
) -> None:
    """A public organization is readable by everyone, so only a writer owns its numbers."""

    async def load() -> tuple[KnowledgeTotals, KnowledgeTotals]:
        await dbutil.reset_db()
        visitor, editor, docs = uuid5(), uuid5(), uuid5()
        await seed_corpus(editor, [docs])
        await dbutil.seed_document(visitor, [visitor])
        standing = OrganizationStanding(id=docs, name="Docs", public=True)
        reading = User.authorized(
            visitor,
            read=(visitor, docs),
            write=(visitor,),
            public=(docs,),
            organizations=(standing,),
        )
        writing = User.authorized(
            editor,
            read=(editor, docs),
            write=(editor, docs),
            public=(docs,),
            organizations=(standing,),
        )
        return (await Dashboard.load(reading)).totals, (await Dashboard.load(writing)).totals

    reader_totals, writer_totals = dbutil.run(load())

    assert reader_totals == KnowledgeTotals(documents=1, files=0, findings=0, subjects=0, themes=0)
    assert writer_totals == KnowledgeTotals(documents=1, files=1, findings=1, subjects=1, themes=1)


def test_a_note_filed_into_a_member_scope_beside_a_public_one_stays_the_callers_own(
    migrated_db: None,
) -> None:
    """Only a row living entirely in unwritable public scopes leaves the counts."""

    async def load() -> KnowledgeTotals:
        await dbutil.reset_db()
        member, lab, docs = uuid5(), uuid5(), uuid5()
        await dbutil.seed_document(member, [member])
        await dbutil.seed_document(member, sorted((lab, docs)))
        await dbutil.seed_document(uuid5(), [docs])
        caller = User.authorized(
            member,
            read=(member, lab, docs),
            write=(member, lab),
            public=(docs,),
            organizations=(
                OrganizationStanding(id=lab, name="Robotics Lab"),
                OrganizationStanding(id=docs, name="Docs", public=True),
            ),
        )
        return (await Dashboard.load(caller)).totals

    assert dbutil.run(load()).documents == 2


def test_the_borrowed_scope_list_is_planned_once_for_the_whole_count(
    migrated_db: None,
) -> None:
    """The exclusion reads the caller's standing, so it must not be re-planned per row."""

    async def plan() -> str:
        await dbutil.reset_db()
        owner, docs = uuid5(), uuid5()
        for _ in range(20):
            await dbutil.seed_document(owner, [owner])
            await dbutil.seed_document(owner, [docs])
        caller = User.authorized(owner, read=(owner, docs), write=(owner,), public=(docs,))
        async with caller as session:
            compiled = Knowledge.totals().compile(
                dialect=session.bind.dialect, compile_kwargs={"literal_binds": True}
            )
            rows = await session.exec(text(f"EXPLAIN {compiled}"))
            return "\n".join(line for (line,) in rows)

    plan_lines = dbutil.run(plan()).splitlines()

    # Every scan of the caller's own scope arrays hangs under an InitPlan, which PostgreSQL
    # runs once per statement. A correlated read would print SubPlan there and pay per row.
    borrowed = [
        index for index, line in enumerate(plan_lines) if "Function Scan on unnest" in line
    ]
    assert borrowed
    assert all("InitPlan" in plan_lines[index - 1] for index in borrowed)


def test_a_cached_page_is_not_counted_as_something_the_caller_wrote(migrated_db: None) -> None:
    """A quarantined page is a stranger's text kept for the next question, not a source."""

    async def load() -> KnowledgeTotals:
        await dbutil.reset_db()
        owner = uuid5()
        await dbutil.seed_document(owner, [owner])
        cached = await dbutil.seed_document(owner, [owner])
        await dbutil.admin_exec(
            "UPDATE document SET origin = 'web_cache' WHERE id = :id",
            {"id": cached},
        )
        return (await Dashboard.load(User.authorized(owner, read=(owner,), write=(owner,)))).totals

    assert dbutil.run(load()).documents == 1
