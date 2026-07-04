import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.extract.models import TimedFact
from aizk.extract.ontology import (
    ONTOLOGY_PROMPT,
    EntityType,
    RelationType,
    check_in_sql,
)

STRUCTURAL_ENTITIES = {EntityType.RAPTOR_SUMMARY, EntityType.OBSERVATION}


@pytest.mark.parametrize("enum", [EntityType, RelationType])
def test_extractable_excludes_structural_and_is_sorted(enum: type) -> None:
    """`extractable` drops the system-written members and returns them byte-stably sorted."""
    extractable = enum.extractable()
    assert extractable == sorted(extractable)
    assert all(not member.structural for member in extractable)
    assert {m for m in enum if m.structural}.isdisjoint(extractable)


def test_structural_members_are_exactly_the_system_written_ones() -> None:
    """Only the RAPTOR/observation types and the observes predicate are structural."""
    assert {m for m in EntityType if m.structural} == STRUCTURAL_ENTITIES
    assert {m for m in RelationType if m.structural} == {RelationType.OBSERVES}


@given(members=st.lists(st.sampled_from(list(RelationType)), min_size=1, max_size=5, unique=True))
def test_check_in_sql_lists_every_member_value(members: list[RelationType]) -> None:
    """The CHECK expression names each member's string value in a quoted `IN (...)` list."""
    sql = check_in_sql("predicate", members)
    assert sql.startswith("predicate IN (")
    for member in members:
        assert f"'{member}'" in sql


def test_ontology_prompt_lists_the_extractable_vocabularies() -> None:
    """The rendered prompt carries every extractable entity type and predicate value."""
    for member in EntityType.extractable():
        assert member.value in ONTOLOGY_PROMPT
    for member in RelationType.extractable():
        assert member.value in ONTOLOGY_PROMPT


def test_timed_fact_object_alias_round_trips() -> None:
    """`TimedFact` accepts and emits the `object` alias while the attribute stays `object_`."""
    fact = TimedFact.model_validate(
        {"subject": "a", "predicate": "uses", "object": "b", "statement": "a uses b"}
    )
    assert fact.object_ == "b"
    assert fact.model_dump(by_alias=True)["object"] == "b"


def test_timed_fact_defaults_to_open_window() -> None:
    """A `TimedFact` left undated carries a null valid window, the always-holding shape."""
    fact = TimedFact(subject="a", predicate=RelationType.USES, statement="s")
    assert fact.valid_from is None and fact.valid_to is None
    assert fact.object_ == ""
