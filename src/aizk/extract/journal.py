import re
from datetime import UTC, datetime

from . import ontology
from .models import ExtractedEntity, TimedFact

# Dated entries may include a parenthesized label after the date.
_JOURNAL_LINE = re.compile(r"^-\s*(\d{4}-\d{2}-\d{2})(?:\s*\([^)]*\))?:\s*(.+)$", re.MULTILINE)

_AREA_TAG = re.compile(r"(?<!\w)#area(?!\w)", re.IGNORECASE)
_PROJECT_TAG = re.compile(r"(?<!\w)#project(?!\w)", re.IGNORECASE)


def has_journal_entries(text: str) -> bool:
    """Whether text contains at least one dated journal entry."""
    return _JOURNAL_LINE.search(text) is not None


def declared_type(text: str) -> str | None:
    """The structural type a note's own tags declare, Area or Project, else None for an
    ordinary note the extractor is left to characterize."""
    if _AREA_TAG.search(text):
        return ontology.AREA
    if _PROJECT_TAG.search(text):
        return ontology.PROJECT
    return None


def title_entity(title: str, declared: str | None) -> ExtractedEntity:
    """The note's own title as an entity, typed by the structural tag it declares or Concept."""
    return ExtractedEntity(name=title, type=declared or ontology.CONCEPT)


def journal_facts(chunk_text: str, title: str) -> list[TimedFact]:
    """Parse a chunk's dated journal lines into facts logged against the note's title entity."""
    facts = []
    for date_text, statement in _JOURNAL_LINE.findall(chunk_text):
        valid_from = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=UTC)
        facts.append(
            TimedFact(
                subject=title,
                predicate=ontology.OBSERVES,
                object="",
                statement=statement.strip(),
                valid_from=valid_from,
                valid_to=None,
            )
        )
    return facts
