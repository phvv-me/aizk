from datetime import datetime

import dateparser
from dateparser.search import search_dates

from .models import TimedFact

# tz-aware results so a resolved valid_from always compares against `FactClaim.valid`'s tstzrange
# on equal footing, and PREFER_DATES_FROM=past since a bare "Tuesday" or "March" in a fact's own
# statement almost always names something that already happened rather than a future date.
#
# PARSERS restricted to absolute-time and STRICT_PARSING on are both load-bearing: dateparser's
# default relative-time parser reads a bare keyword like "today" or "now" out of an arbitrary
# sentence and resolves it to the current instant, and even absolute-time alone still accepts a
# bare clock time ("06:00:00") as if it were a date, so unrestricted settings stamp a spurious
# valid_from onto facts that name no date at all (caught live: "The EntityGate is a 205M CPU-fast
# pre-filter..." resolved to today's date with no date anywhere in the text). Requiring a complete
# day-month-year match keeps every explicit and embedded date this cascade actually needs
# (`2024-03-15`, `On 2024-03-15 the team decided...`, a journal line's own `YYYY-MM-DD`) while
# rejecting prose, prices, times, and bare years; the one known loss is a bare month-and-year with
# no day ("March 2024"), which the LLM's own date field normalizes to a full date in practice.
DATEPARSER_SETTINGS = {
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DATES_FROM": "past",
    "PARSERS": ["absolute-time"],
    "STRICT_PARSING": True,
}


def parse_date(text: str) -> datetime | None:
    """Parse a date out of free text with no LLM call, or return null when the text names none.

    Tries `dateparser.parse` first, the fast path for a string that is itself mostly a date (the
    combined extraction call's own per-fact date field), then falls back to
    `dateparser.search.search_dates` for a date embedded inside a longer sentence (a fact's own
    statement), returning the earliest match found.

    text: the string to search for a date, empty when there is nothing to search.
    """
    if not text:
        return None
    direct = dateparser.parse(text, settings=DATEPARSER_SETTINGS)
    if direct is not None:
        return direct
    found = search_dates(text, settings=DATEPARSER_SETTINGS)
    return found[0][1] if found else None


def resolve_valid_from(explicit: str | None, statement: str) -> datetime | None:
    """Resolve one fact's valid_from from its own text with no LLM call.

    The combined extraction call's own date field wins when the model named one; otherwise a date
    parsed out of the fact's own statement text; null when neither carries one. The final
    fallback, the source document's own timestamp, is `with_document_fallback`'s concern once a
    whole chunk's facts are in hand, since only the caller knows the document.

    explicit: the combined call's own per-fact date field, null when the model named no date.
    statement: the fact's self-contained statement, searched for an embedded date as the second
        tier.
    """
    return parse_date(explicit or "") or parse_date(statement)


def with_document_fallback(
    facts: list[TimedFact], document_created_at: datetime
) -> list[TimedFact]:
    """Fill every still-undated fact's valid_from with the source document's own timestamp.

    The dating cascade's final, always-available tier: a fact whose statement and whose own date
    field both named nothing still gets a valid_from rather than staying undated forever, the
    document's own creation time standing in for "known to hold since this was recorded."

    facts: the chunk's dated candidate facts, some possibly still carrying a null valid_from.
    document_created_at: the source document's own creation timestamp, the fallback value.
    """
    return [
        fact
        if fact.valid_from is not None
        else fact.model_copy(update={"valid_from": document_created_at})
        for fact in facts
    ]
