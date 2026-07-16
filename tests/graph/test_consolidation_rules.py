import pytest
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5, uuid7
from pydantic import UUID5, UUID7

from aizk.config import settings
from aizk.extract.models import ConsolidationVerdict
from aizk.graph.consolidation import Consolidator, FactMatch, cosine_similarity
from aizk.store import Relation


def match(
    similarity: float,
    object_id: UUID5 | UUID7 | None = None,
) -> FactMatch:
    return FactMatch(
        id=uuid7(),
        statement="existing",
        object_id=object_id,
        distance=1.0 - similarity,
    )


# Integer components keep cosine norms away from underflow.
finite = st.integers(min_value=-1000, max_value=1000).map(float)


@given(vector=st.lists(finite, min_size=1, max_size=6))
def test_cosine_is_reflexive_symmetric_bounded_and_orthogonal(vector: list[float]) -> None:
    if any(component != 0 for component in vector):
        assert cosine_similarity(vector, vector) == pytest.approx(1.0)
    else:
        assert cosine_similarity(vector, vector) == 0.0
    shifted = [component + 1.0 for component in vector]
    assert cosine_similarity(vector, shifted) == pytest.approx(cosine_similarity(shifted, vector))
    assert -1.0001 <= cosine_similarity(vector, shifted) <= 1.0001
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_decide_by_rule_handles_empty_and_borderline_similarity_bands() -> None:
    floor = settings.consolidation_borderline_floor
    auto = settings.consolidation_auto_merge_threshold
    below = floor - 0.05
    band = (floor + auto) / 2
    rules = Consolidator()
    assert rules.decide(Relation.Policy.state, None, []) == ConsolidationVerdict(action="ADD")
    assert rules.decide(Relation.Policy.state, None, [match(below)]) == ConsolidationVerdict(
        action="NOOP"
    )
    assert rules.decide(Relation.Policy.set, None, [match(below)]) == ConsolidationVerdict(
        action="ADD"
    )
    assert rules.decide(Relation.Policy.set, None, [match(band)]) == ConsolidationVerdict(
        action="ADD"
    )


def test_decide_by_rule_auto_merge_noop_update_and_add() -> None:
    auto = settings.consolidation_auto_merge_threshold
    rules = Consolidator()
    obj = uuid5()
    best = match(auto, object_id=obj)
    assert rules.decide(Relation.Policy.state, obj, [best]) == ConsolidationVerdict(action="NOOP")
    other = uuid5()
    verdict = rules.decide(Relation.Policy.state, other, [best])
    assert verdict == ConsolidationVerdict(action="UPDATE", supersedes=best.id)
    assert rules.decide(Relation.Policy.set, obj, [best]) == ConsolidationVerdict(action="NOOP")
    assert rules.decide(Relation.Policy.set, other, [best]) == ConsolidationVerdict(action="ADD")
    assert rules.decide(Relation.Policy.event, other, [best]) == ConsolidationVerdict(action="ADD")
