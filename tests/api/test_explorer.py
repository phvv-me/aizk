from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import dbutil
import pytest
from factories import seed_artifact
from id_factory import uuid5, uuid7, uuid8
from pydantic import UUID5

from aizk.api.explorer import (
    FindingRecord,
    FindingView,
    GraphRecord,
    GraphSlice,
    NameRecord,
    SourcePage,
    SourceView,
    SubjectRecord,
    SubjectView,
    ThemeView,
)
from aizk.store import Community, Document
from aizk.store.identity import OrganizationStanding, User


def caller() -> tuple[User, list[UUID5]]:
    """Build one caller and its private plus organization scopes."""
    owner, team = uuid5(), uuid5()
    return (
        User.authorized(
            owner,
            read=(owner, team),
            organizations=(OrganizationStanding(id=team, name="Lab"),),
        ),
        [owner, team, owner],
    )


def test_catalog_views_present_fallbacks_and_deduplicated_scope_labels() -> None:
    user, scopes = caller()
    now = datetime.now(UTC)
    source = SourceView.from_row(
        Document(
            title=None,
            subject_type=None,
            source_uri=None,
            content_hash=uuid8(),
            created_by=user.id,
            scopes=scopes,
        ),
        user,
    )
    finding = FindingView.from_record(
        FindingRecord(
            id=uuid7(),
            statement="A relates to B",
            predicate="related_to",
            subject_id=uuid5(),
            subject_name="A",
            object_id=uuid5(),
            object_name="B",
            recorded_at=now,
            source_id=None,
            source_title=None,
            scopes=scopes,
        ),
        user,
    )
    subject = SubjectView.from_record(
        SubjectRecord(
            id=uuid7(),
            content_id=uuid5(),
            name="A",
            type="Concept",
            scopes=scopes,
            updated_at=now,
            finding_count=2,
        ),
        user,
    )

    assert (source.title, source.kind, source.origin, source.source_uri) == (
        "Untitled source",
        "Source",
        "document",
        "",
    )
    assert source.scopes == finding.scopes == subject.scopes == ("Private", "Lab")


def test_source_catalog_separates_authored_documents_from_files(migrated_db: None) -> None:
    async def load() -> tuple[SourcePage, SourcePage, SourcePage]:
        await dbutil.reset_db()
        owner = uuid5()
        authored = await dbutil.seed_document(owner, [owner])
        stored = await seed_artifact(owner, [owner], name="paper.pdf")
        file_document = await dbutil.seed_document(owner, [owner])
        await dbutil.admin_exec(
            "UPDATE document SET artifact_id = :artifact, artifact_content_id = :content "
            "WHERE id = :document",
            {
                "artifact": stored.artifact.id,
                "content": stored.content.id,
                "document": file_document,
            },
        )
        await dbutil.admin_exec(
            "UPDATE document SET title = :title WHERE id = :document",
            {"title": "Authored note", "document": authored},
        )
        user = User.authorized(owner, read=(owner,))
        return (
            await SourcePage.load(user),
            await SourcePage.load(user, origin="document"),
            await SourcePage.load(user, origin="file"),
        )

    all_sources, documents, files = dbutil.run(load())

    assert all_sources.total == 2
    assert [(row.title, row.origin) for row in documents.rows] == [("Authored note", "document")]
    assert [row.origin for row in files.rows] == ["file"]


def test_theme_view_loads_bounded_member_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, scopes = caller()
    names = (NameRecord(name="Alpha"), NameRecord(name="Beta"))
    execute = AsyncMock(return_value=names)
    fake_exec = MagicMock()
    fake_exec.__getitem__.return_value = execute
    monkeypatch.setattr(User, "exec", property(lambda _: fake_exec))
    row = Community(
        label="Connected ideas",
        summary="A compact theme",
        member_ids=[uuid5(), uuid5()],
        created_by=user.id,
        scopes=scopes,
    )

    view = dbutil.run(ThemeView.from_row(row, user))

    assert view.members == ("Alpha", "Beta")
    assert view.member_count == 2
    assert view.scopes == ("Private", "Lab")
    execute.assert_awaited_once()


def test_graph_slice_bounds_edges_and_accumulates_node_degrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, _ = caller()
    alpha, beta, gamma = uuid5(), uuid5(), uuid5()
    rows = (
        GraphRecord(
            subject_id=alpha,
            subject_name="Alpha",
            object_id=beta,
            object_name="Beta",
            predicate="uses",
            statement="Alpha uses Beta",
        ),
        GraphRecord(
            subject_id=alpha,
            subject_name="Alpha",
            object_id=gamma,
            object_name="Gamma",
            predicate="supports",
            statement="Alpha supports Gamma",
        ),
    )
    execute = AsyncMock(return_value=rows)
    fake_exec = MagicMock()
    fake_exec.__getitem__.return_value = execute
    monkeypatch.setattr(User, "exec", property(lambda _: fake_exec))

    graph = dbutil.run(GraphSlice.load(user, limit=1))

    assert graph.truncated is True
    assert len(graph.edges) == 1
    assert [(node.label, node.degree) for node in graph.nodes] == [
        ("Alpha", 1),
        ("Beta", 1),
    ]
    execute.assert_awaited_once()
