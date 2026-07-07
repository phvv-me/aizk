import re
from datetime import UTC, datetime

from . import ontology
from .models import ExtractedEntity, TimedFact

# a dated journal line, the vault's own convention for a project's dated entries, `- YYYY-MM-DD:
# text` or `- YYYY-MM-DD (note): text`, the parenthetical a free-form time-of-day or label the
# line's own statement already carries in full and this parse never needs, anchored per line so
# one pass over a chunk's whole text finds every entry it carries.
JOURNAL_LINE = re.compile(r"^-\s*(\d{4}-\d{2}-\d{2})(?:\s*\([^)]*\))?:\s*(.+)$", re.MULTILINE)

# the structural tags a note's own text carries to declare its title entity's type, an area
# container or a project, over the default Concept. Matched as whole words so a title or body
# merely mentioning the word in prose never flips it, and area is tested first since an area note
# lists its projects and so may mention both while being itself the container.
AREA_TAG = re.compile(r"(?<!\w)#area(?!\w)", re.IGNORECASE)
PROJECT_TAG = re.compile(r"(?<!\w)#project(?!\w)", re.IGNORECASE)


def declared_type(text: str) -> str | None:
    """The structural type a note's own tags declare, Area or Project, else None for an ordinary
    note the extractor is left to characterize.

    This is the trust-declared-structure seam: a note that names its own kind is believed rather
    than guessed, which is why Area and Project are system-written types the LLM never emits, so a
    projects or areas roster is exactly the notes that declared themselves rather than whatever a
    small model over-tagged.

    text: text to scan, typically every chunk of one document concatenated or checked in turn.
    """
    if AREA_TAG.search(text):
        return ontology.AREA
    if PROJECT_TAG.search(text):
        return ontology.PROJECT
    return None


def title_entity(title: str, declared: str | None) -> ExtractedEntity:
    """The note's own title as an entity, typed by the structural tag it declares or Concept.

    title: the note's title, its canonical surface form as an entity.
    declared: the type the note's tags declare, Area or Project, or None for the default Concept.
    """
    return ExtractedEntity(name=title, type=declared or ontology.CONCEPT)


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
                predicate=ontology.OBSERVES,
                object="",
                statement=statement.strip(),
                valid_from=valid_from,
                valid_to=None,
            )
        )
    return facts
