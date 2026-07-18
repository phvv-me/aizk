from datetime import datetime

from patos import FrozenModel
from pydantic import ConfigDict

from ..config import settings
from ..store import Document, Knowledge, Usage
from ..store.identity import User


class View(FrozenModel):
    """Base for browser API views whose serialized defaults are part of the wire contract.

    Defaulted fields are always present in responses, so the OpenAPI serialization
    schema marks them required and the generated TypeScript client keeps them
    non-optional.
    """

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class KnowledgeTotals(View):
    """Human-facing totals for the knowledge visible to one caller."""

    sources: int = 0
    findings: int = 0
    subjects: int = 0
    themes: int = 0


class UsageTotals(View):
    """Caller-owned operation and transfer totals suitable for cost awareness."""

    recalls: int = 0
    remembers: int = 0
    files: int = 0
    uploaded_bytes: int = 0
    downloaded_bytes: int = 0


class ScopedRow(View):
    """Shared dated, scope-labeled presentation row for the api dashboards."""

    source_uri: str = ""
    date: str
    scopes: tuple[str, ...]

    @staticmethod
    def format_date(value: datetime) -> str:
        """Format one row date compactly for the dashboard."""
        return value.strftime("%b %d, %Y").replace(" 0", " ")


class RecentSource(ScopedRow):
    """Presentation metadata for one source without exposing internal identifiers."""

    title: str
    kind: str


class Dashboard(FrozenModel):
    """Personal knowledge overview loaded entirely through caller-bound RLS reads."""

    totals: KnowledgeTotals = KnowledgeTotals()
    usage: UsageTotals = UsageTotals()
    recent_sources: tuple[RecentSource, ...] = ()

    @classmethod
    async def load(
        cls,
        user: User,
        source_limit: int = settings.web_recent_source_limit,
    ) -> Dashboard:
        """Load visible totals and source metadata through caller-bound `User.exec` reads."""
        (counts,) = await user.exec[KnowledgeTotals](Knowledge.totals())
        (usage,) = await user.exec[UsageTotals](Usage.Event.totals())
        rows = await user.exec[Document](Document.newest(source_limit))
        return cls(
            totals=counts,
            usage=usage,
            recent_sources=tuple(
                RecentSource(
                    title=row.title or "Untitled source",
                    source_uri=row.source_uri or "",
                    kind=(row.subject_type or "source").replace("_", " ").title(),
                    date=RecentSource.format_date(row.observed_at or row.updated_at),
                    scopes=tuple(dict.fromkeys(user.scope_labels(row.scopes))),
                )
                for row in rows
            ),
        )
