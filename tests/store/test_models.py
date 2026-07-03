import uuid
from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.extract.ontology import EntityType, RelationType
from aizk.store import EntityContent, FactContent
from aizk.store.mixins import Scoped

# the full closed vocabulary each ORM validator admits, every EntityType/RelationType member,
# the extractor vocabulary plus the structural types the system writes outside extraction
ADMITTED_ENTITY_TYPES = st.sampled_from(sorted(EntityType))
ADMITTED_PREDICATES = st.sampled_from(sorted(RelationType))


@given(entity_type=ADMITTED_ENTITY_TYPES)
def test_entity_type_validator_admits_the_whole_ontology(entity_type: str) -> None:
    """Every ontology entity type, the structural summary type included, passes the second wall."""
    assert EntityContent(name="x", type=entity_type).type == entity_type


@given(predicate=ADMITTED_PREDICATES)
def test_fact_predicate_validator_admits_the_whole_ontology(predicate: str) -> None:
    """Every closed-vocabulary relation type passes the fact predicate validator unchanged."""
    fact = FactContent(subject_id=uuid.uuid4(), predicate=predicate, statement="x")
    assert fact.predicate == predicate


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda value: EntityContent(name="x", type=value), id="entity-type"),
        pytest.param(
            lambda value: FactContent(subject_id=uuid.uuid4(), predicate=value, statement="x"),
            id="fact-predicate",
        ),
    ],
)
def test_orm_validators_reject_off_vocabulary(
    build: Callable[[str], EntityContent | FactContent],
) -> None:
    """A type or predicate outside the closed ontology is refused at construction, the second wall.

    build: constructs the model under test from one off-vocabulary string.
    """
    with pytest.raises(ValueError, match="ontology"):
        build("definitely not in the ontology")


def test_scoped_registration_skips_a_subclass_without_a_tablename() -> None:
    """A non-mapped Scoped subclass carrying no __tablename__ never lands in the rls registry.

    The mixin only records concrete tables, so an abstract intermediate that defines no table
    name is skipped, leaving the registry to the seven real scoped tables.
    """
    before = set(Scoped.__subclasses__())

    class Intermediate(Scoped):
        pass

    assert Intermediate not in before
    assert not hasattr(Intermediate, "__tablename__")
