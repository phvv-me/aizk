from datetime import UTC, date, datetime

from hypothesis import given
from hypothesis import strategies as st

from aizk.extract import ontology
from aizk.extract.journal import (
    declared_type,
    has_journal_entries,
    journal_facts,
    title_entity,
)

dates = st.dates(min_value=datetime(2000, 1, 1).date(), max_value=datetime(2099, 12, 31).date())
line_text = st.text(alphabet=st.characters(blacklist_characters="\n"), min_size=1, max_size=40)


@given(day=dates, body=line_text)
def test_dated_line_parses_to_one_observed_fact(day: date, body: str) -> None:
    facts = journal_facts(f"- {day.isoformat()}: {body}", title="My Note")
    assert len(facts) == 1
    fact = facts[0]
    assert fact.subject == "My Note"
    assert fact.predicate == ontology.OBSERVES
    assert fact.statement == body.strip()
    assert fact.valid_from == datetime(day.year, day.month, day.day, tzinfo=UTC)
    assert fact.valid_to is None


@given(day=dates, label=st.text(alphabet="abc ", min_size=1, max_size=8), body=line_text)
def test_parenthetical_label_is_dropped_from_the_statement(
    day: date, body: str, label: str
) -> None:
    facts = journal_facts(f"- {day.isoformat()} ({label}): {body}", title="t")
    assert len(facts) == 1
    assert facts[0].statement == body.strip()


@given(count=st.integers(min_value=0, max_value=6))
def test_line_count_matches_fact_count(count: int) -> None:
    lines = [f"- 2021-03-0{index + 1}: entry {index}" for index in range(count)]
    noise = ["not a journal line", "## heading", "- undated bullet"]
    text = "\n".join(noise + lines)
    assert len(journal_facts(text, title="t")) == count


def test_malformed_dates_and_prose_never_match() -> None:
    assert journal_facts("we shipped on 2021-03-01 finally", title="t") == []
    assert journal_facts("- 2021-3-1: bad month width", title="t") == []


@given(
    text=st.text(alphabet=st.characters(blacklist_characters="#"), max_size=40),
    tagged=st.booleans(),
)
def test_project_tag_flips_the_title_entity_type(text: str, tagged: bool) -> None:
    body = f"{text} #project" if tagged else text
    assert declared_type(body) == (ontology.PROJECT if tagged else None)
    entity = title_entity("Title", declared_type(body))
    assert entity.type == (ontology.PROJECT if tagged else ontology.CONCEPT)


def test_area_tag_declares_area_and_wins_over_project() -> None:
    assert declared_type("the research hub #area") == ontology.AREA
    assert declared_type("#area with #project members listed") == ontology.AREA
    assert title_entity("Research", declared_type("x #area")).type == ontology.AREA


def test_structural_words_in_prose_never_flip() -> None:
    assert declared_type("this project is great") is None
    assert declared_type("see #projections for detail") is None
    assert declared_type("the bay area is sunny") is None


def test_journal_line_regex_is_anchored_per_line() -> None:
    assert has_journal_entries("- 2021-01-01: a\n- 2021-01-02: b") is True
