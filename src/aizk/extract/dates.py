from datetime import datetime

import dateparser
from dateparser.search import search_dates

from .models import TimedFact

_SETTINGS = {
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DATES_FROM": "past",
    "PARSERS": ["absolute-time"],
    "STRICT_PARSING": True,
    "DATE_ORDER": "YMD",
}


def parse_date(text: str) -> datetime | None:
    """Parse a strict absolute date from free text when one is present."""
    if not text:
        return None
    direct = dateparser.parse(text, settings=_SETTINGS)
    if direct is not None:
        return direct
    found = search_dates(text, settings=_SETTINGS)
    return min(date for _, date in found) if found else None


def resolve_valid_from(explicit: str | None, statement: str) -> datetime | None:
    """Prefer a fact's explicit date and then inspect its statement."""
    return parse_date(explicit or "") or parse_date(statement)


def with_source_fallback(
    facts: list[TimedFact],
    observed_at: datetime,
    expires_at: datetime | None = None,
) -> list[TimedFact]:
    """Fill undated facts and cap open claims at the source expiry."""
    return [
        fact.model_copy(
            update={
                "valid_from": fact.valid_from or observed_at,
                "valid_to": fact.valid_to or expires_at,
            }
        )
        for fact in facts
    ]
