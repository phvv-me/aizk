from math import ceil

from hypothesis import given
from hypothesis import strategies as st

from aizk.retrieval import Candidate, Lane
from aizk.retrieval.packing import pack

CHARS_PER_TOKEN = 4.0


def candidates_strategy() -> st.SearchStrategy[list[Candidate]]:
    return st.lists(
        st.builds(
            Candidate,
            lane=st.sampled_from(list(Lane.Kind)),
            line=st.text(max_size=40),
        ),
        max_size=12,
    )


def oracle(candidates: list[Candidate], budget: int) -> tuple[list[Candidate], int]:
    """The walk's law replayed independently: keep the prefix while each line plus one
    separator still fits the budget, stopping at the first that does not."""
    used, kept = 0, []
    for candidate in candidates:
        cost = ceil(len(candidate.line) / CHARS_PER_TOKEN) + 1
        if used + cost > budget:
            break
        used += cost
        kept.append(candidate)
    return kept, used


@given(candidates=candidates_strategy(), budget=st.integers(min_value=0, max_value=200))
def test_pack_walk_matches_the_prefix_budget_oracle(
    candidates: list[Candidate], budget: int
) -> None:
    kept, used = pack(candidates, budget, CHARS_PER_TOKEN)

    expected_kept, expected_used = oracle(candidates, budget)
    assert list(kept) == expected_kept
    assert used == expected_used


@given(candidates=candidates_strategy(), budget=st.integers(min_value=0, max_value=200))
def test_pack_keeps_an_exact_prefix_inside_the_budget(
    candidates: list[Candidate], budget: int
) -> None:
    kept, used = pack(candidates, budget, CHARS_PER_TOKEN)

    assert used <= budget
    assert not kept or used > 0
    assert list(kept) == candidates[: len(kept)]
