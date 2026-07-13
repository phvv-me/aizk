import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.config import settings
from aizk.extract.models import ConsolidationVerdict
from aizk.graph.consolidation import (
    FactMatch,
    cosine_similarity,
    decide_by_rule,
    same_predicate_verdict,
)


def match(
    similarity: float,
    predicate: str = "uses",
    object_id: uuid.UUID | None = None,
) -> FactMatch:
    return FactMatch(
        id=uuid.uuid4(),
        statement="existing",
        predicate=predicate,
        object_id=object_id,
        distance=1.0 - similarity,
    )


# Integer components keep cosine norms away from underflow.
finite = st.integers(min_value=-1000, max_value=1000).map(float)


@given(vector=st.lists(finite, min_size=1, max_size=6))
def test_cosine_of_a_vector_with_itself_is_one(vector: list[float]) -> None:
    if any(component != 0 for component in vector):
        assert cosine_similarity(vector, vector) == pytest.approx(1.0)
    else:
        assert cosine_similarity(vector, vector) == 0.0


@given(a=st.lists(finite, min_size=1, max_size=6))
def test_cosine_is_symmetric_and_bounded(a: list[float]) -> None:
    b = [component + 1.0 for component in a]
    assert cosine_similarity(a, b) == pytest.approx(cosine_similarity(b, a))
    assert -1.0001 <= cosine_similarity(a, b) <= 1.0001


def test_orthogonal_vectors_score_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_decide_by_rule_add_when_no_similar_claims() -> None:
    assert decide_by_rule("uses", None, []) == ConsolidationVerdict(action="ADD")


def test_decide_by_rule_similarity_bands() -> None:
    floor = settings.consolidation_borderline_floor
    auto = settings.consolidation_auto_merge_threshold
    below = floor - 0.05
    band = (floor + auto) / 2
    assert decide_by_rule("uses", None, [match(below)]) == ConsolidationVerdict(action="ADD")
    assert decide_by_rule("uses", None, [match(band)]) is None


def test_decide_by_rule_auto_merge_noop_update_and_add() -> None:
    auto = settings.consolidation_auto_merge_threshold
    obj = uuid.uuid4()
    best = match(auto, object_id=obj)
    assert decide_by_rule("uses", obj, [best]) == ConsolidationVerdict(action="NOOP")
    other = uuid.uuid4()
    verdict = decide_by_rule("uses", other, [best])
    assert verdict == ConsolidationVerdict(action="UPDATE", supersedes=best.id)
    assert decide_by_rule("depends_on", obj, [best]) == ConsolidationVerdict(action="ADD")

    different = match(auto, predicate="depends_on", object_id=obj)
    assert same_predicate_verdict("uses", obj, [different]) == ConsolidationVerdict(action="ADD")
    assert same_predicate_verdict(
        "uses",
        obj,
        [different, match(settings.consolidation_borderline_floor - 0.05, object_id=obj)],
    ) == ConsolidationVerdict(action="ADD")
    borderline = (settings.consolidation_borderline_floor + auto) / 2
    assert (
        same_predicate_verdict("uses", obj, [different, match(borderline, object_id=obj)]) is None
    )
    assert same_predicate_verdict("uses", obj, [different, best]) == (
        ConsolidationVerdict(action="NOOP")
    )
    assert same_predicate_verdict("uses", other, [different, best]) == (
        ConsolidationVerdict(action="UPDATE", supersedes=best.id)
    )
