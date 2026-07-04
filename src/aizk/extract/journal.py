import re
from datetime import UTC, datetime

from .models import ExtractedEntity, TimedFact
from .ontology import EntityType, RelationType

# a dated journal line, the vault's own convention for a project's dated entries, `- YYYY-MM-DD:
# text` or `- YYYY-MM-DD (note): text`, the parenthetical a free-form time-of-day or label the
# line's own statement already carries in full and this parse never needs, anchored per line so
# one pass over a chunk's whole text finds every entry it carries.
JOURNAL_LINE = re.compile(r"^-\s*(\d{4}-\d{2}-\d{2})(?:\s*\([^)]*\))?:\s*(.+)$", re.MULTILINE)

# the tag a note's own text carries to mark its title entity as a Project rather than the default
# Concept, matched as a whole word so a title or body merely mentioning "project" in prose never
# flips it.
PROJECT_TAG = re.compile(r"(?<!\w)#project(?!\w)", re.IGNORECASE)


def is_tagged_project(text: str) -> bool:
    """Whether a note's own text carries the #project tag, anywhere in the document.

    text: text to scan, typically every chunk of one document concatenated or checked in turn.
    """
    return bool(PROJECT_TAG.search(text))


def title_entity(title: str, tagged_project: bool) -> ExtractedEntity:
    """The note's own title as the subject entity every journal line in it is logged against.

    title: the note's title, its canonical surface form as an entity.
    tagged_project: whether the note carries the #project tag, Project over the default Concept.
    """
    return ExtractedEntity(
        name=title, type=EntityType.PROJECT if tagged_project else EntityType.CONCEPT
    )


def journal_facts(chunk_text: str, title: str) -> list[TimedFact]:
    """Parse a chunk's dated journal lines into facts logged against the note's title entity.

    Deterministic, no LLM call: each `- YYYY-MM-DD: text` line becomes one fact whose subject is
    the note's own title, predicate the system-written observes relation (the same one the
    reflective insight pass stamps, a write-back never extracted from prose), statement the line
    text verbatim, and valid_from the parsed date with no valid_to, an open-ended entry that still
    holds. This is the events table a `timeline` read later renders as dated lines.

    chunk_text: one chunk's span, scanned line by line for the journal convention.
    title: the note's title, the subject every parsed line is logged against.
    """
    facts = []
    for date_text, statement in JOURNAL_LINE.findall(chunk_text):
        valid_from = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=UTC)
        facts.append(
            TimedFact(
                subject=title,
                predicate=RelationType.OBSERVES,
                object="",
                statement=statement.strip(),
                valid_from=valid_from,
                valid_to=None,
            )
        )
    return facts
