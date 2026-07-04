from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from aizk.extract.journal import (
    JOURNAL_LINE,
    is_tagged_project,
    journal_facts,
    title_entity,
)
from aizk.extract.ontology import EntityType, RelationType

dates = st.dates(min_value=datetime(2000, 1, 1).date(), max_value=datetime(2099, 12, 31).date())
line_text = st.text(alphabet=st.characters(blacklist_characters="\n"), min_size=1, max_size=40)


@given(day=dates, body=line_text)
def test_dated_line_parses_to_one_observed_fact(day: str, body: str) -> None:
    """A `- YYYY-MM-DD: text` line yields one observed fact dated to the parsed day, verbatim."""
    facts = journal_facts(f"- {day.isoformat()}: {body}", title="My Note")
    assert len(facts) == 1
    fact = facts[0]
    assert fact.subject == "My Note"
    assert fact.predicate == RelationType.OBSERVES
    assert fact.statement == body.strip()
    assert fact.valid_from == datetime(day.year, day.month, day.day, tzinfo=UTC)
    assert fact.valid_to is None


@given(day=dates, label=st.text(alphabet="abc ", min_size=1, max_size=8), body=line_text)
def test_parenthetical_label_is_dropped_from_the_statement(
    day: str, body: str, label: str
) -> None:
    """The optional `(label)` between the date and colon is ignored, never part of a statement."""
    facts = journal_facts(f"- {day.isoformat()} ({label}): {body}", title="t")
    assert len(facts) == 1
    assert facts[0].statement == body.strip()


@given(count=st.integers(min_value=0, max_value=6))
def test_line_count_matches_fact_count(count: int) -> None:
    """Every dated line becomes exactly one fact, and non-journal lines contribute none."""
    lines = [f"- 2021-03-0{index + 1}: entry {index}" for index in range(count)]
    noise = ["not a journal line", "## heading", "- undated bullet"]
    text = "\n".join(noise + lines)
    assert len(journal_facts(text, title="t")) == count


def test_malformed_dates_and_prose_never_match() -> None:
    """A prose mention of a date or a malformed line yields no journal facts."""
    assert journal_facts("we shipped on 2021-03-01 finally", title="t") == []
    assert journal_facts("- 2021-3-1: bad month width", title="t") == []


@given(
    text=st.text(max_size=40),
    tagged=st.booleans(),
)
def test_project_tag_flips_the_title_entity_type(text: str, tagged: bool) -> None:
    """`#project` as a whole word makes the title a Project entity, otherwise a Concept."""
    body = f"{text} #project" if tagged else text
    assert is_tagged_project(body) is tagged
    entity = title_entity("Title", is_tagged_project(body))
    assert entity.type == (EntityType.PROJECT if tagged else EntityType.CONCEPT)


def test_project_word_in_prose_does_not_flip() -> None:
    """A bare `project` or `#projection` never trips the whole-word `#project` tag."""
    assert not is_tagged_project("this project is great")
    assert not is_tagged_project("see #projections for detail")


def test_journal_line_regex_is_anchored_per_line() -> None:
    """The line pattern is multiline-anchored so it finds every entry across a chunk."""
    assert len(JOURNAL_LINE.findall("- 2021-01-01: a\n- 2021-01-02: b")) == 2
