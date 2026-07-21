import asyncio

import pytest
from doubles import FakeLLM
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5, uuid7
from pydantic import UUID5, UUID7

from aizk.config import settings
from aizk.extract.models import BatchConsolidationVerdict, ConsolidationVerdict, TimedFact
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


@pytest.mark.parametrize(
    ("policy", "similarity", "same_object", "match_count", "action"),
    [
        (Relation.Policy.state, None, True, 0, "ADD"),
        (Relation.Policy.state, 0.2, True, 1, "NOOP"),
        (Relation.Policy.state, 0.2, False, 1, "UPDATE"),
        (Relation.Policy.state, 0.95, True, 2, "UPDATE"),
        (Relation.Policy.set, settings.consolidation_borderline_floor - 0.05, True, 1, "ADD"),
        (Relation.Policy.set, settings.consolidation_borderline_floor, True, 1, None),
        (
            Relation.Policy.event,
            (settings.consolidation_borderline_floor + settings.consolidation_auto_merge_threshold)
            / 2,
            False,
            1,
            None,
        ),
        (Relation.Policy.set, settings.consolidation_auto_merge_threshold, True, 1, "NOOP"),
        (Relation.Policy.event, settings.consolidation_auto_merge_threshold, True, 1, "NOOP"),
        (Relation.Policy.set, settings.consolidation_auto_merge_threshold, False, 1, "ADD"),
    ],
)
def test_decide_respects_policy_and_similarity_bands(
    policy: Relation.Policy,
    similarity: float | None,
    same_object: bool,
    match_count: int,
    action: str | None,
) -> None:
    rules = Consolidator(llm=FakeLLM().llm)
    obj = uuid5()
    matches = (
        []
        if similarity is None
        else [match(similarity, obj if same_object else uuid5()) for _ in range(match_count)]
    )

    verdict = rules.decide(policy, obj, matches)

    if action is None:
        assert verdict is None
    else:
        assert verdict is not None
        assert verdict.action == action
        assert verdict.supersedes == (matches[0].id if action == "UPDATE" else None)


@pytest.mark.parametrize(
    "scenario",
    ["empty", "known-update", "missing", "unknown-update", "noop-target", "add-target"],
)
def test_resolve_batches_and_normalizes_model_verdicts(scenario: str) -> None:
    fake = FakeLLM()
    existing = match(0.8)
    candidate = TimedFact(subject="Subject", predicate="uses", statement="new")
    if scenario == "empty":
        candidates: list[tuple[TimedFact, list[FactMatch]]] = []
        expected: list[ConsolidationVerdict] = []
    else:
        candidates = [(candidate, [existing])]
        responses = {
            "known-update": ConsolidationVerdict(action="UPDATE", supersedes=existing.id),
            "unknown-update": ConsolidationVerdict(action="UPDATE", supersedes=uuid7()),
            "noop-target": ConsolidationVerdict(action="NOOP", supersedes=uuid7()),
            "add-target": ConsolidationVerdict(action="ADD", supersedes=uuid7()),
        }
        response = responses.get(scenario)
        fake.register(
            BatchConsolidationVerdict,
            BatchConsolidationVerdict(verdicts=[] if response is None else [response]),
        )
        if scenario == "known-update":
            assert response is not None
            expected = [response]
        else:
            expected = [
                ConsolidationVerdict(action="NOOP" if scenario == "noop-target" else "ADD")
            ]

    assert asyncio.run(Consolidator(llm=fake.llm).resolve(candidates)) == expected
    assert len(fake.completions.calls) == (0 if scenario == "empty" else 1)
