from datetime import datetime
from typing import Literal

from patos import FrozenModel
from pydantic import UUID5, UUID7

from ..store import Community, Document, Explorer
from ..store.identity import User
from .dashboard import View

type SourceOrigin = Literal["all", "document", "file"]


class CountRecord(FrozenModel):
    """One labeled count row returned by a catalog query."""

    total: int


class SourceView(View):
    """One visible source with stable navigation and provenance metadata."""

    id: UUID7
    title: str
    kind: str
    origin: Literal["document", "file"]
    source_uri: str = ""
    observed_at: datetime | None = None
    updated_at: datetime
    scopes: tuple[str, ...]

    @classmethod
    def from_row(cls, row: Document, user: User) -> SourceView:
        """Present one source with human-readable scope labels."""
        return cls(
            id=row.id,
            title=row.title or "Untitled source",
            kind=(row.subject_type or "source").replace("_", " ").title(),
            origin="file" if row.artifact_id is not None else "document",
            source_uri=row.source_uri or "",
            observed_at=row.observed_at,
            updated_at=row.updated_at,
            scopes=tuple(dict.fromkeys(user.scope_labels(row.scopes))),
        )


class SourcePage(View):
    """One bounded source catalog page."""

    total: int
    offset: int
    limit: int
    origin: SourceOrigin
    rows: tuple[SourceView, ...] = ()

    @classmethod
    async def load(
        cls,
        user: User,
        search: str = "",
        origin: SourceOrigin = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> SourcePage:
        """Load visible source rows and their matching total."""
        (count,) = await user.exec[CountRecord](Explorer.source_total(search, origin))
        rows = await user.exec[Document](Explorer.source_rows(search, origin, limit, offset))
        return cls(
            total=count.total,
            offset=offset,
            limit=limit,
            origin=origin,
            rows=tuple(SourceView.from_row(row, user) for row in rows),
        )


class FindingRecord(FrozenModel):
    """Database-shaped finding row with joined navigation labels."""

    id: UUID7
    statement: str
    predicate: str
    subject_id: UUID5
    subject_name: str
    object_id: UUID5 | None
    object_name: str | None
    recorded_at: datetime
    source_id: UUID7 | None
    source_title: str | None
    scopes: list[UUID5]


class FindingView(View):
    """One visible current finding and its graph endpoints."""

    id: UUID7
    statement: str
    predicate: str
    subject_id: UUID5
    subject_name: str
    object_id: UUID5 | None = None
    object_name: str | None = None
    recorded_at: datetime
    source_id: UUID7 | None = None
    source_title: str | None = None
    scopes: tuple[str, ...]

    @classmethod
    def from_record(cls, record: FindingRecord, user: User) -> FindingView:
        """Present one finding with human-readable scope labels."""
        return cls(
            **record.model_dump(exclude={"scopes"}),
            scopes=tuple(dict.fromkeys(user.scope_labels(record.scopes))),
        )


class FindingPage(View):
    """One bounded chronological finding page."""

    total: int
    offset: int
    limit: int
    rows: tuple[FindingView, ...] = ()

    @classmethod
    async def load(
        cls, user: User, search: str = "", limit: int = 50, offset: int = 0
    ) -> FindingPage:
        """Load visible current findings and their matching total."""
        (count,) = await user.exec[CountRecord](Explorer.finding_total(search))
        rows = await user.exec[FindingRecord](Explorer.finding_rows(search, limit, offset))
        return cls(
            total=count.total,
            offset=offset,
            limit=limit,
            rows=tuple(FindingView.from_record(row, user) for row in rows),
        )


class SubjectRecord(FrozenModel):
    """Database-shaped visible subject claim."""

    id: UUID7
    content_id: UUID5
    name: str
    type: str
    scopes: list[UUID5]
    updated_at: datetime
    finding_count: int


class SubjectView(View):
    """One visible subject claim with its current graph degree."""

    id: UUID7
    content_id: UUID5
    name: str
    type: str
    updated_at: datetime
    finding_count: int
    scopes: tuple[str, ...]

    @classmethod
    def from_record(cls, record: SubjectRecord, user: User) -> SubjectView:
        """Present one subject with human-readable scope labels."""
        return cls(
            **record.model_dump(exclude={"scopes"}),
            scopes=tuple(dict.fromkeys(user.scope_labels(record.scopes))),
        )


class SubjectPage(View):
    """One bounded subject catalog page."""

    total: int
    offset: int
    limit: int
    rows: tuple[SubjectView, ...] = ()

    @classmethod
    async def load(
        cls, user: User, search: str = "", limit: int = 50, offset: int = 0
    ) -> SubjectPage:
        """Load visible subject claims and their matching total."""
        (count,) = await user.exec[CountRecord](Explorer.subject_total(search))
        rows = await user.exec[SubjectRecord](Explorer.subject_rows(search, limit, offset))
        return cls(
            total=count.total,
            offset=offset,
            limit=limit,
            rows=tuple(SubjectView.from_record(row, user) for row in rows),
        )


class NameRecord(FrozenModel):
    """One canonical entity name selected by a visible theme."""

    name: str


class ThemeView(View):
    """One visible graph community with a bounded member preview."""

    id: UUID7
    label: str
    summary: str
    member_count: int
    members: tuple[str, ...] = ()
    updated_at: datetime
    scopes: tuple[str, ...]

    @classmethod
    async def from_row(cls, row: Community, user: User) -> ThemeView:
        """Present one theme with its first member names and scope labels."""
        names = await user.exec[NameRecord](Explorer.member_names(row.member_ids))
        return cls(
            id=row.id,
            label=row.label,
            summary=row.summary,
            member_count=len(row.member_ids),
            members=tuple(name.name for name in names),
            updated_at=row.updated_at,
            scopes=tuple(dict.fromkeys(user.scope_labels(row.scopes))),
        )


class ThemePage(View):
    """Every visible graph theme ordered by membership size."""

    rows: tuple[ThemeView, ...] = ()

    @classmethod
    async def load(cls, user: User) -> ThemePage:
        """Load visible themes and bounded member previews."""
        rows = await user.exec[Community](Explorer.theme_rows())
        return cls(rows=tuple([await ThemeView.from_row(row, user) for row in rows]))


class GraphRecord(FrozenModel):
    """Database-shaped binary finding for the bounded graph view."""

    subject_id: UUID5
    subject_name: str
    object_id: UUID5
    object_name: str
    predicate: str
    statement: str


class GraphNode(View):
    """One subject node in the deterministic relationship graph."""

    id: UUID5
    label: str
    degree: int


class GraphEdge(View):
    """One labeled current finding connecting two subject nodes."""

    source: UUID5
    target: UUID5
    predicate: str
    statement: str


class GraphSlice(View):
    """One hard-bounded latest-finding graph with an accessible edge list."""

    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    truncated: bool = False

    @classmethod
    async def load(cls, user: User, limit: int = 40) -> GraphSlice:
        """Build one graph slice from the newest visible binary findings."""
        rows = await user.exec[GraphRecord](Explorer.graph_rows(limit + 1))
        truncated = len(rows) > limit
        edges = tuple(
            GraphEdge(
                source=row.subject_id,
                target=row.object_id,
                predicate=row.predicate,
                statement=row.statement,
            )
            for row in rows[:limit]
        )
        labels: dict[UUID5, str] = {}
        degrees: dict[UUID5, int] = {}
        for row in rows[:limit]:
            labels[row.subject_id] = row.subject_name
            labels[row.object_id] = row.object_name
            degrees[row.subject_id] = degrees.get(row.subject_id, 0) + 1
            degrees[row.object_id] = degrees.get(row.object_id, 0) + 1
        nodes = tuple(
            GraphNode(id=node_id, label=label, degree=degrees[node_id])
            for node_id, label in sorted(
                labels.items(), key=lambda item: (-degrees[item[0]], item[1])
            )
        )
        return cls(nodes=nodes, edges=edges, truncated=truncated)
