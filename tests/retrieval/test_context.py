import asyncio
import uuid
from collections.abc import Awaitable, Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st
from strategies import recall_results

import aizk.retrieval.context as context_module
from aizk.config import settings
from aizk.retrieval import (
    Block,
    ContextPack,
    FactHit,
    Hit,
    RecallResult,
    SessionNote,
    assemble_context_pack,
    context_blocks,
    estimate_tokens,
    pack_context,
)
from aizk.retrieval.context import LANE_ORDER, block_cost, source_block


def recompute_used(blocks: list[Block]) -> int:
    """The token cost `fit_to_budget` accumulates, recomputed to pin `used_tokens` to `block_cost`.

    blocks: the kept blocks, in the order the pack laid them down.
    """
    used = 0
    opened: set[str] = set()
    for block in blocks:
        used += block_cost(block, opened)
        opened.add(block.lane)
    return used


@given(text=st.text(max_size=400))
def test_estimate_tokens_is_a_ceiling_that_never_undercounts(text: str) -> None:
    """The token estimate is a ceiling at four characters per token, so it never undercounts."""
    tokens = estimate_tokens(text)
    assert tokens * context_module.CHARS_PER_TOKEN >= len(text)
    assert tokens == -(-len(text) // context_module.CHARS_PER_TOKEN)


@pytest.mark.parametrize(
    ("title", "uri", "shown"),
    [("Paper", "u.md", "Paper"), (None, "u.md", "u.md"), (None, None, "untitled")],
)
def test_source_block_titles_by_title_then_uri_then_untitled(
    title: str | None, uri: str | None, shown: str
) -> None:
    """A source block falls back title to uri to untitled, its snippet whitespace-collapsed."""
    block = source_block(
        Hit(document_title=title, source_uri=uri, text="  a\t b   c  ", score=0.5)
    )
    assert block.lane == "sources"
    assert block.line.startswith(f"[0.5] {shown}")
    assert "a b c"[: settings.snippet_chars] in block.line


@given(result=recall_results())
def test_context_blocks_lay_lanes_out_broad_first_one_line_per_item(
    result: RecallResult,
) -> None:
    """Every populated item is one block, laid out in the fixed broad-first lane priority."""
    blocks = context_blocks(result)
    expected = (
        (1 if result.profile else 0)
        + len(result.raptor)
        + len(result.communities)
        + len(result.facts)
        + len(result.session)
        + len(result.hits)
    )
    assert len(blocks) == expected
    first_seen: dict[str, int] = {}
    for index, block in enumerate(blocks):
        first_seen.setdefault(block.lane, index)
    positions = [
        LANE_ORDER.index(lane) for lane, _ in sorted(first_seen.items(), key=lambda k: k[1])
    ]
    assert positions == sorted(positions)


@given(result=recall_results(), token_budget=st.integers(min_value=1, max_value=4000))
def test_pack_context_keeps_a_budget_bound_prefix(result: RecallResult, token_budget: int) -> None:
    """The pack keeps a strict prefix of the ordered blocks and never overruns its own budget."""
    pack = pack_context(result, token_budget)
    assert isinstance(pack, ContextPack)
    all_blocks = context_blocks(result)
    assert pack.blocks == all_blocks[: len(pack.blocks)]
    assert pack.budget == token_budget
    assert pack.used_tokens <= token_budget
    assert pack.used_tokens == recompute_used(pack.blocks)


@given(result=recall_results())
def test_pack_context_with_room_keeps_every_block(result: RecallResult) -> None:
    """Given a budget past the whole cost, the pack keeps every block in laid-down order."""
    all_blocks = context_blocks(result)
    generous = recompute_used(all_blocks) + 1
    pack = pack_context(result, generous)
    assert pack.blocks == all_blocks
    assert pack.used_tokens == recompute_used(all_blocks)


def a_bundle() -> RecallResult:
    """A recall bundle with a lane at each priority, the fixed input the budget example packs."""
    return RecallResult(
        query="what holds",
        profile="Leech lattice is the optimal packing in dimension 24.",
        raptor=[],
        communities=[],
        facts=[
            FactHit(
                statement="Alice authored the paper.",
                predicate="related_to",
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


def test_pack_context_drops_the_low_priority_lanes_under_a_tight_budget() -> None:
    """A tight budget keeps the broad view first while the raw sources fall off the end."""
    result = a_bundle()
    lanes = [block.lane for block in pack_context(result, 4000).blocks]
    assert lanes.index("profile") < lanes.index("facts") < lanes.index("sources")

    tight = pack_context(result, estimate_tokens(result.profile or "") + 4)
    kept = {block.lane for block in tight.blocks}
    assert "profile" in kept
    assert "sources" not in kept
    assert tight.used_tokens <= tight.budget


def stub_recall_into(
    captured: dict[str, object],
) -> Callable[[str, uuid.UUID, int, tuple[uuid.UUID, ...]], Awaitable[RecallResult]]:
    """A recall stand-in that records its arguments into `captured`, no database touched.

    captured: the dict the returned stub writes the call's keyword arguments into.
    """

    async def stub(
        query: str, principal_id: uuid.UUID, k: int, scopes: tuple[uuid.UUID, ...]
    ) -> RecallResult:
        captured.update(query=query, principal_id=principal_id, k=k, scopes=scopes)
        return a_bundle()

    return stub


def test_assemble_context_pack_reuses_recall_and_packs_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pack entrypoint recalls once under the principal and scopes, then packs the lanes."""
    captured: dict[str, object] = {}
    principal = uuid.uuid4()
    monkeypatch.setattr(context_module, "recall", stub_recall_into(captured))
    pack = asyncio.run(
        assemble_context_pack("what holds", principal_id=principal, token_budget=4000, k=5)
    )
    assert captured == {"query": "what holds", "principal_id": principal, "k": 5, "scopes": ()}
    lanes = {block.lane for block in pack.blocks}
    assert {"profile", "facts", "working memory"} <= lanes


def test_assemble_context_pack_defaults_principal_and_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A null principal and budget fall back to the configured system principal and ceiling."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(context_module, "recall", stub_recall_into(captured))
    pack = asyncio.run(assemble_context_pack("what holds"))
    assert captured["principal_id"] == settings.system_user_id
    assert pack.budget == settings.context_token_budget
    assert pack.used_tokens <= settings.context_token_budget
