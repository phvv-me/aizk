from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.extract.dates import parse_date, resolve_valid_from, with_source_fallback
from aizk.extract.models import TimedFact


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2024-03-15", (2024, 3, 15)),
        ("On 2020-01-15 the team decided to ship", (2020, 1, 15)),
    ],
)
def test_absolute_dates_parse_with_timezone(
    text: str,
    expected: tuple[int, int, int],
) -> None:
    parsed = parse_date(text)
    assert parsed is not None and parsed.tzinfo is not None
    assert (parsed.year, parsed.month, parsed.day) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no date here at all",  # prose
        "today",  # a relative keyword, rejected with the relative parser off
        "now",
        "06:00:00",  # a bare clock time, rejected under STRICT_PARSING
        "2024",  # a bare year with no day/month
        "a plain sentence about nothing",
    ],
)
def test_non_date_text_parses_to_none(text: str) -> None:
    assert parse_date(text) is None


@pytest.mark.parametrize(
    ("explicit", "statement", "expected"),
    [
        ("2019-05-06", "mentions 2022-11-12 in passing", (2019, 5, 6)),
        (None, "shipped on 2018-02-03 finally", (2018, 2, 3)),
        (None, "no date in this prose", None),
    ],
)
def test_valid_from_prefers_explicit_then_statement_dates(
    explicit: str | None,
    statement: str,
    expected: tuple[int, int, int] | None,
) -> None:
    resolved = resolve_valid_from(explicit, statement)
    assert (
        None if resolved is None else (resolved.year, resolved.month, resolved.day)
    ) == expected


@given(explicit_start=st.booleans(), explicit_end=st.booleans())
def test_document_window_fills_only_open_fact_bounds(
    explicit_start: bool,
    explicit_end: bool,
) -> None:
    doc_time = datetime(2020, 6, 1, tzinfo=UTC)
    start = datetime(2015, 1, 1, tzinfo=UTC)
    expiry = datetime(2020, 7, 1, tzinfo=UTC)
    end = datetime(2020, 6, 15, tzinfo=UTC)
    fact = TimedFact(
        subject="a",
        predicate="uses",
        statement="x",
        valid_from=start if explicit_start else None,
        valid_to=end if explicit_end else None,
    )

    [filled] = with_source_fallback([fact], doc_time, expiry)

    assert filled.valid_from == (start if explicit_start else doc_time)
    assert filled.valid_to == (end if explicit_end else expiry)
