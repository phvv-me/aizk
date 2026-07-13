from datetime import UTC, datetime

import pytest

from aizk.extract.dating import parse_date, resolve_valid_from, with_document_fallback
from aizk.extract.models import TimedFact


def test_empty_text_parses_to_none() -> None:
    assert parse_date("") is None


def test_direct_iso_date_parses_timezone_aware() -> None:
    parsed = parse_date("2024-03-15")
    assert parsed is not None and parsed.tzinfo is not None
    assert (parsed.year, parsed.month, parsed.day) == (2024, 3, 15)


def test_embedded_date_is_found_by_the_search_fallback() -> None:
    parsed = parse_date("On 2020-01-15 the team decided to ship")
    assert parsed is not None and (parsed.year, parsed.month, parsed.day) == (2020, 1, 15)


@pytest.mark.parametrize(
    "text",
    [
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


def test_resolve_valid_from_prefers_explicit_over_statement() -> None:
    resolved = resolve_valid_from("2019-05-06", "mentions 2022-11-12 in passing")
    assert resolved is not None and (resolved.year, resolved.month) == (2019, 5)


def test_resolve_valid_from_falls_back_to_the_statement() -> None:
    resolved = resolve_valid_from(None, "shipped on 2018-02-03 finally")
    assert resolved is not None and resolved.year == 2018


def test_resolve_valid_from_is_none_when_neither_carries_a_date() -> None:
    assert resolve_valid_from(None, "no date in this prose") is None


def test_document_fallback_fills_only_undated_facts() -> None:
    doc_time = datetime(2020, 6, 1, tzinfo=UTC)
    kept = datetime(2015, 1, 1, tzinfo=UTC)
    facts = [
        TimedFact(subject="a", predicate="uses", statement="x", valid_from=kept),
        TimedFact(subject="b", predicate="uses", statement="y", valid_from=None),
    ]
    filled = with_document_fallback(facts, doc_time)
    assert filled[0].valid_from == kept
    assert filled[1].valid_from == doc_time
    assert all(fact.valid_from is not None for fact in filled)
