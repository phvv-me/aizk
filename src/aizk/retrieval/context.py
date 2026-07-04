import uuid

from mainboard.profiling import span

from ..config import settings
from .lanes import recall
from .models import Block, ContextPack, Hit, RecallResult

# characters per token the budget estimate assumes, the common rule of thumb that one token runs
# about four characters of English, so the pack sizes itself without loading a real tokenizer.
CHARS_PER_TOKEN = 4

# the lanes the pack lays down in widening-to-narrowing priority, each a header and the section it
# reads off the recall bundle, so the budget fills the broad view first and the raw sources last.
LANE_ORDER = ("profile", "overview", "communities", "facts", "working memory", "sources")


def estimate_tokens(text: str) -> int:
    """Approximate the token count of a string at the assumed characters-per-token rate.

    A cheap deterministic estimate the pack sizes itself against, rounding up so a line is never
    undercounted and the assembled pack stays within its budget.

    text: the string to size.
    """
    return -(-len(text) // CHARS_PER_TOKEN)


def source_block(hit: Hit) -> Block:
    """Render one chunk hit as a `sources` lane block, a score-prefixed title over its snippet.

    hit: the fused chunk hit to render.
    """
    title = hit.document_title or hit.source_uri or "untitled"
    snippet = " ".join(hit.text.split())[: settings.snippet_chars]
    return Block(lane="sources", line=f"[{round(hit.score, 3)}] {title}\n  {snippet}")


def context_blocks(result: RecallResult) -> list[Block]:
    """Render a recall bundle as ordered Block lines, the budget fills in priority order.

    Each populated lane contributes one line per item under its header, laid out broad view first
    so the pack keeps the community and RAPTOR overview, then the facts and working items, then the
    raw sources, dropping the later lanes first when the budget runs out.

    result: the fused recall bundle to lay out.
    """
    lanes: list[list[Block]] = [
        [Block(lane="profile", line=result.profile)] if result.profile else [],
        [
            Block(lane="overview", line=f"- L{n.level} {n.label}: {n.summary}")
            for n in result.raptor
        ],
        [Block(lane="communities", line=f"- {n.label}: {n.summary}") for n in result.communities],
        [Block(lane="facts", line=f"- ({f.predicate}) {f.statement}") for f in result.facts],
        [Block(lane="working memory", line=f"- [{s.kind}] {s.text}") for s in result.session],
        [source_block(hit) for hit in result.hits],
    ]
    return [block for lane in lanes for block in lane]


def block_cost(block: Block, opened: set[str]) -> int:
    """The token cost of keeping one more block, its line plus its header the first time it opens.

    block: the candidate block a budget fill is considering.
    opened: lanes already charged their header cost earlier in the same fill.
    """
    header = estimate_tokens(f"{block.lane}:") if block.lane not in opened else 0
    return header + estimate_tokens(block.line) + 1


def fit_to_budget(blocks: list[Block], token_budget: int) -> tuple[list[Block], int]:
    """Keep the leading blocks that fit inside token_budget, stopping at the first that would not.

    A block's own priority order already reflects the widening-to-narrowing lane order, so keeping
    a strict prefix rather than picking and choosing preserves that order in the packed result.

    blocks: candidate blocks in priority order.
    token_budget: the token ceiling the kept blocks must stay within.
    """
    used = 0
    kept: list[Block] = []
    opened: set[str] = set()
    for block in blocks:
        cost = block_cost(block, opened)
        if used + cost > token_budget:
            break
        used += cost
        opened.add(block.lane)
        kept.append(block)
    return kept, used


@span
def pack_context(result: RecallResult, token_budget: int) -> ContextPack:
    """Fit the recalled lanes into a token-budgeted `ContextPack`, broad view first, sources last.

    result: the fused recall bundle to pack.
    token_budget: the token ceiling the assembled pack must stay within.
    """
    kept, used = fit_to_budget(context_blocks(result), token_budget)
    return ContextPack(query=result.query, blocks=kept, budget=token_budget, used_tokens=used)


async def assemble_context_pack(
    query: str,
    principal_id: uuid.UUID | None = None,
    token_budget: int | None = None,
    k: int = 8,
    scopes: tuple[uuid.UUID, ...] = (),
) -> ContextPack:
    """Recall for a query and pack the fused lanes into a token-budgeted `ContextPack`.

    Reuses recall, which already opens and scopes its own database session, so the pack rides
    the same fused facts, profiles, community and RAPTOR summaries, and working items a recall
    surfaces, then fits them to the budget. The one prompt-ready assembly an agent reads without
    choosing the lane mix itself.

    query: what to assemble context about.
    principal_id: identity whose row level security visibility scopes the recall, the system
        principal when null.
    token_budget: the token ceiling, the configured default when null.
    k: how many hits and seed facts the underlying recall surfaces.
    scopes: group ids narrowing the read to that combination's composed graph, the whole visible
        union when empty.
    """
    principal_id = principal_id or settings.system_principal_id
    result = await recall(query, principal_id=principal_id, k=k, scopes=scopes)
    return pack_context(result, token_budget or settings.context_token_budget)
