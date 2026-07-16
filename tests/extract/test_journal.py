from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.extract.declaration import SourceDeclaration, journal_facts
from aizk.ontology import Ontology, System

dates = st.dates(min_value=datetime(2000, 1, 1).date(), max_value=datetime(2099, 12, 31).date())
line_text = st.text(alphabet=st.characters(blacklist_characters="\n"), min_size=1, max_size=40)


@given(day=dates, body=line_text)
def test_dated_line_parses_to_one_observed_fact(day: date, body: str) -> None:
    facts = journal_facts(f"- {day.isoformat()}: {body}", title="My Note")
    assert len(facts) == 1
    fact = facts[0]
    assert fact.subject == "My Note"
    assert fact.predicate == System.Relation.OBSERVES
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


def test_generic_source_declaration_parses_typed_relations() -> None:
    declared = SourceDeclaration.from_text(
        "# Aizk\n\n- Type Project\n- part_of [Area] [[Productivity|Productivity Area]]\n"
        "- has_status [Status] Active"
    )

    assert declared.title == "Aizk"
    assert declared.subject_type == "Project"
    assert [
        (relation.predicate, relation.object_type, relation.object_name)
        for relation in declared.relations
    ] == [
        ("part_of", "Area", "Productivity"),
        ("has_status", "Status", "Active"),
    ]


def test_management_words_without_type_remain_an_ordinary_note() -> None:
    declared = SourceDeclaration.from_text("# Notes\n- Status Active\nThis project is great")

    assert declared.subject_type is None
    assert declared.relations == ()


def test_ordinary_declaration_is_already_canonical_and_extracts_nothing(
    migrated_db: None,
) -> None:
    declared = SourceDeclaration.from_text("# Notes\nOrdinary prose")
    ontology = Ontology.current()

    assert declared.canonical(ontology) is declared
    assert declared.extraction(ontology, datetime(2026, 7, 16, tzinfo=UTC), None).facts == []


def test_typed_source_requires_a_title() -> None:
    with pytest.raises(ValueError, match="level-one Markdown title"):
        SourceDeclaration.from_text("- Type Project\n- has_status [Status] Active")


def test_journal_lines_are_anchored_per_line() -> None:
    assert len(journal_facts("- 2021-01-01: a\n- 2021-01-02: b", "title")) == 2
