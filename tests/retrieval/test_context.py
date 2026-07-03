import asyncio
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st
from strategies import recall_results

import aizk.retrieval.context as context_module
from aizk.retrieval import (
    FactHit,
    Hit,
    RecallResult,
    SessionNote,
    assemble_context_pack,
    estimate_tokens,
    pack_context,
)


@given(text=st.text(max_size=400))
def test_estimate_tokens_rounds_up_and_never_undercounts(text: str) -> None:
    """The token estimate is a ceiling at four characters per token, so it never undercounts."""
    tokens = estimate_tokens(text)
    assert tokens * context_module.CHARS_PER_TOKEN >= len(text)
    assert tokens - 1 < len(text) / context_module.CHARS_PER_TOKEN <= tokens or not text


@given(result=recall_results(), token_budget=st.integers(min_value=1, max_value=4000))
def test_pack_context_never_exceeds_the_budget(result: RecallResult, token_budget: int) -> None:
    """Whatever the bundle, the assembled pack fits the budget or is the lone no-memory line.

    The budget invariant get_context rests on, that the running header-and-line total the packer
    stops at bounds the rendered string, so a token-budgeted assembly always fits its window.
    """
    pack = pack_context(result, token_budget)
    assert estimate_tokens(pack) <= token_budget or pack.startswith("no memory recalled")


def a_bundle() -> RecallResult:
    """A recall bundle with a lane at each priority, the fixed input the budget test packs."""
    return RecallResult(
        query="what holds",
        profile="Leech lattice is the optimal packing in dimension 24.",
        raptor=[],
        communities=[],
        facts=[
            FactHit(
                statement="Alice authored the paper.",
                predicate="because",
                score=0.9,
                valid_from=None,
                valid_to=None,
            )
        ],
        session=[SessionNote(text="a fresh decision worth keeping", kind="note", score=0.8)],
        hits=[
            Hit(document_title="src", source_uri="s.md", text="a supporting passage", score=0.5)
        ],
        as_of=None,
    )


def test_pack_context_keeps_priority_lanes_and_drops_the_low_ones_under_a_tight_budget() -> None:
    """A tight budget keeps the broad view first and the raw sources fall off the end.

    The lane priority the pack reads top to bottom, the profile and facts kept while the lowest
    sources lane is dropped once the budget runs out, and the whole pack stays within the budget.
    """
    result = a_bundle()
    assert result.profile is not None  # the bundle always carries a profile lane
    full = pack_context(result, 4000)
    assert "profile:" in full and "sources:" in full  # a large budget carries every lane

    tight = pack_context(result, estimate_tokens(result.profile) + 4)
    assert estimate_tokens(tight) <= estimate_tokens(result.profile) + 4
    assert "profile:" in tight  # the highest-priority lane survives
    assert "sources:" not in tight  # the lowest-priority lane is dropped first


def test_assemble_context_pack_reuses_recall_and_packs_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pack entrypoint recalls once under the principal and renders the budgeted lanes."""
    captured: dict[str, object] = {}

    async def stub_recall(
        query: str, principal_id: uuid.UUID, k: int, scope: uuid.UUID | None = None
    ) -> RecallResult:
        captured["query"] = query
        captured["principal_id"] = principal_id
        captured["scope"] = scope
        return a_bundle()

    principal = uuid.uuid4()
    monkeypatch.setattr(context_module, "recall", stub_recall)
    pack = asyncio.run(
        assemble_context_pack("what holds", principal_id=principal, token_budget=4000)
    )
    assert captured == {"query": "what holds", "principal_id": principal, "scope": None}
    assert "profile:" in pack and "facts:" in pack and "working memory:" in pack
