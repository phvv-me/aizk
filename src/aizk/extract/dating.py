from datetime import datetime

import dateparser
from dateparser.search import search_dates

from .models import TimedFact

# Strict absolute YMD parsing rejects incidental times, relative words, prices, and bare years.
_DATEPARSER_SETTINGS = {
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DATES_FROM": "past",
    "PARSERS": ["absolute-time"],
    "STRICT_PARSING": True,
    "DATE_ORDER": "YMD",
}


def parse_date(text: str) -> datetime | None:
    """Parse a date out of free text with no LLM call, or return null when the text names
    none."""
    if not text:
        return None
    direct = dateparser.parse(text, settings=_DATEPARSER_SETTINGS)
    if direct is not None:
        return direct
    found = search_dates(text, settings=_DATEPARSER_SETTINGS)
    if not found:
        return None
    return min(date for _, date in found)


def resolve_valid_from(explicit: str | None, statement: str) -> datetime | None:
    """Resolve one fact's valid_from from its own text with no LLM call."""
    return parse_date(explicit or "") or parse_date(statement)


def with_document_fallback(
    facts: list[TimedFact], document_created_at: datetime
) -> list[TimedFact]:
    """Fill every still-undated fact's valid_from with the source document's own timestamp."""
    return [
        fact
        if fact.valid_from is not None
        else fact.model_copy(update={"valid_from": document_created_at})
        for fact in facts
    ]
