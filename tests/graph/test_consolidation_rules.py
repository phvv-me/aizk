import uuid
from types import SimpleNamespace

import pytest
from factories import build_live_fact
from hypothesis import given
from hypothesis import strategies as st

from aizk.config import settings
from aizk.extract.models import ConsolidationVerdict
from aizk.graph.consolidation import cosine_similarity, decide_by_rule, rank_pool


class _Vec:
    """A dense-vector stand-in exposing the `.to_list()` a DB-loaded halfvec carries."""

    def __init__(self, values: list[float]) -> None:
        self._values = values

    def to_list(self) -> list[float]:
        """The vector as a plain list, what `rank_pool` reads off each claim's embedding."""
        return self._values


def claim(statement: str, embedding: list[float] | None) -> object:
    """A duck-typed live-claim carrying just the fields `rank_pool` reads, embedding included.

    `LiveFact` now validates its `list[float]` embedding field, so a real halfvec object can't ride
    through its constructor; this stand-in carries the `.to_list()` seam `rank_pool` needs instead.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        statement=statement,
        predicate="uses",
        object_id=None,
        embedding=None if embedding is None else _Vec(embedding),
    )


# integer-derived components keep magnitudes well away from the float-underflow floor `x*x` would
# hit for a tiny-but-nonzero value, so cosine's own denominator never collapses to zero spuriously.
finite = st.integers(min_value=-1000, max_value=1000).map(float)


@given(vector=st.lists(finite, min_size=1, max_size=6))
def test_cosine_of_a_vector_with_itself_is_one(vector: list[float]) -> None:
    """A non-zero vector is perfectly similar to itself; a zero vector scores zero, never NaN."""
    if any(component != 0 for component in vector):
        assert cosine_similarity(vector, vector) == pytest.approx(1.0)
    else:
        assert cosine_similarity(vector, vector) == 0.0


@given(a=st.lists(finite, min_size=1, max_size=6))
def test_cosine_is_symmetric_and_bounded(a: list[float]) -> None:
    """Cosine similarity is symmetric and stays within the unit interval it is defined on."""
    b = [component + 1.0 for component in a]
    assert cosine_similarity(a, b) == pytest.approx(cosine_similarity(b, a))
    assert -1.0001 <= cosine_similarity(a, b) <= 1.0001


def test_orthogonal_vectors_score_zero() -> None:
    """Two orthogonal vectors carry no shared direction, so their similarity is zero."""
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_rank_pool_sorts_desc_skips_unembedded_and_caps() -> None:
    """The pool ranks by similarity, drops unembedded claims, and caps at `similar_facts`."""
    query = [1.0, 0.0]
    near = claim(statement="near", embedding=[0.99, 0.14])
    far = claim(statement="far", embedding=[0.14, 0.99])
    unembedded = claim(statement="none", embedding=None)
    ranked = rank_pool(query, [far, near, unembedded])
    assert [scored.statement for scored, _ in ranked] == ["near", "far"]
    assert all(scored.embedding is not None for scored, _ in ranked)
    assert len(rank_pool(query, [near] * (settings.similar_facts + 3))) == settings.similar_facts


def test_decide_by_rule_add_when_no_similar_claims() -> None:
    """A subject with no existing claim is a trivial ADD, no LLM deferral."""
    assert decide_by_rule("uses", None, []) == ConsolidationVerdict(action="ADD")


def test_decide_by_rule_similarity_bands() -> None:
    """A top match below the floor is ADD; between floor and auto-merge is a deferral (null)."""
    floor = settings.consolidation_borderline_floor
    auto = settings.consolidation_auto_merge_threshold
    below = floor - 0.05
    band = (floor + auto) / 2
    best = build_live_fact(predicate="uses", object_id=None)
    assert decide_by_rule("uses", None, [(best, below)]) == (ConsolidationVerdict(action="ADD"))
    assert decide_by_rule("uses", None, [(best, band)]) is None


def test_decide_by_rule_auto_merge_noop_update_and_add() -> None:
    """At or above auto-merge: same p+o is NOOP, same p new o is UPDATE, new p is ADD."""
    auto = settings.consolidation_auto_merge_threshold
    obj = uuid.uuid4()
    best = build_live_fact(predicate="uses", object_id=obj)
    # same predicate and object: a near-duplicate, NOOP
    assert decide_by_rule("uses", obj, [(best, auto)]) == (ConsolidationVerdict(action="NOOP"))
    # same predicate, different object: the value changed, UPDATE superseding the old claim
    other = uuid.uuid4()
    verdict = decide_by_rule("uses", other, [(best, auto)])
    assert verdict == ConsolidationVerdict(action="UPDATE", supersedes=best.id)
    # different predicate: a genuinely different assertion, ADD
    assert decide_by_rule("depends_on", obj, [(best, auto)]) == (
        ConsolidationVerdict(action="ADD")
    )
