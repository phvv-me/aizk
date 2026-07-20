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


def test_generic_tags_declare_the_subject_and_associate_other_typed_entities() -> None:
    declared = SourceDeclaration.from_text(
        "# AIZK Productization\n\n#project: AIZK Productization\n#area: Business"
    )

    assert declared.subject_type == "project"
    assert [(tag.object_type, tag.object_name) for tag in declared.tags] == [
        ("project", "AIZK Productization"),
        ("area", "Business"),
    ]
    extracted = declared.extraction(Ontology.current(), datetime(2026, 7, 16, tzinfo=UTC), None)
    assert [(entity.name, entity.type) for entity in extracted.entities] == [
        ("AIZK Productization", "project"),
        ("Business", "area"),
    ]
    assert [(fact.predicate, fact.object_) for fact in extracted.facts] == [
        (System.Relation.RELATED_TO, "Business")
    ]


def test_supporting_note_tags_use_the_generic_related_to_relation() -> None:
    declared = SourceDeclaration.from_text(
        "# Ontology boundary\n\n#project: AIZK Productization\n#area: Business"
    )

    assert declared.subject_type is None
    extracted = declared.extraction(Ontology.current(), datetime(2026, 7, 16, tzinfo=UTC), None)
    assert [(entity.name, entity.type) for entity in extracted.entities] == [
        ("Ontology boundary", System.Entity.CONCEPT),
        ("AIZK Productization", "project"),
        ("Business", "area"),
    ]
    assert [(fact.predicate, fact.object_) for fact in extracted.facts] == [
        (System.Relation.RELATED_TO, "AIZK Productization"),
        (System.Relation.RELATED_TO, "Business"),
    ]


def test_self_tags_reject_conflicting_subject_kinds() -> None:
    with pytest.raises(ValueError, match="conflicting ontology kinds"):
        SourceDeclaration.from_text("# AIZK\n\n#project: AIZK\n#area: AIZK")


def test_generic_tag_kind_must_exist_in_the_live_ontology() -> None:
    declared = SourceDeclaration.from_text("# Finding\n\n#imaginary kind: AIZK")

    with pytest.raises(ValueError, match="unknown ontology entity type"):
        declared.canonical(Ontology.current())


def test_management_words_without_type_remain_an_ordinary_note() -> None:
    declared = SourceDeclaration.from_text("# Notes\n- Status Active\nThis project is great")

    assert declared.subject_type is None
    assert declared.tags == ()
    assert declared.relations == ()


def test_type_like_bullets_in_imported_content_are_not_declarations() -> None:
    declared = SourceDeclaration.from_text(
        "# swebok-v4.pdf\n\n"
        "## Source file\n\n"
        "Conversion state ready\n\n"
        "## Extracted content\n\n"
        "- Type (classification or category of the requirement)\n"
        "- part_of [Area] Requirements engineering"
    )

    assert declared.title == "swebok-v4.pdf"
    assert declared.subject_type is None
    assert declared.tags == ()
    assert declared.relations == ()
    assert declared.canonical(Ontology.current()) is declared


def test_prose_closes_the_leading_declaration_block() -> None:
    declared = SourceDeclaration.from_text(
        "# Notes\n\nOrdinary authored prose.\n\n- Type Project\n#area: Productivity"
    )

    assert declared.subject_type is None
    assert declared.tags == ()
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
