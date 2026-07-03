import uuid

from ..config import settings
from .models import Block, RecallResult
from .recall import recall

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


def context_blocks(result: RecallResult) -> list[Block]:
    """Render a recall bundle as ordered Block lines, the budget fills in priority order.

    Each populated lane contributes one line per item under its header, laid out broad view first
    so the pack keeps the community and RAPTOR overview, then the facts and working items, then the
    raw sources, dropping the later lanes first when the budget runs out.

    result: the fused recall bundle to lay out.
    """
    blocks: list[Block] = []
    if result.profile:
        blocks.append(Block(lane="profile", line=result.profile))
    blocks += [
        Block(lane="overview", line=f"- L{n.level} {n.label}: {n.summary}") for n in result.raptor
    ]
    blocks += [
        Block(lane="communities", line=f"- {n.label}: {n.summary}") for n in result.communities
    ]
    blocks += [Block(lane="facts", line=f"- ({f.predicate}) {f.statement}") for f in result.facts]
    blocks += [Block(lane="working memory", line=f"- [{s.kind}] {s.text}") for s in result.session]
    blocks += [
        Block(
            lane="sources",
            line=(
                f"[{round(h.score, 3)}] {h.document_title or h.source_uri or 'untitled'}\n"
                f"  {' '.join(h.text.split())[: settings.snippet_chars]}"
            ),
        )
        for h in result.hits
    ]
    return blocks


def pack_context(result: RecallResult, token_budget: int) -> str:
    """Assemble the recalled lanes into a prompt-ready pack that fits inside the token budget.

    Lays the lanes down in priority order and keeps each block while it fits, stopping at the
    first block that would cross the budget so the whole pack is always under it, then groups the
    kept blocks by lane under their headers in the order the lanes first appear. An empty recall
    renders the one no-context line rather than a bundle of blank sections.

    result: the fused recall bundle to pack.
    token_budget: the token ceiling the assembled pack must stay within.
    """
    used = 0
    kept: list[Block] = []
    opened: set[str] = set()
    for block in context_blocks(result):
        # a lane's header is charged the first time it opens, and one token covers the newline
        # that joins the line in, so the running total is an upper bound on the rendered pack and
        # a total under budget guarantees the assembled string is too.
        header = estimate_tokens(f"{block.lane}:") if block.lane not in opened else 0
        cost = header + estimate_tokens(block.line) + 1
        if used + cost > token_budget:
            break
        used += cost
        opened.add(block.lane)
        kept.append(block)
    if not kept:
        return f"no memory recalled for {result.query!r}"
    sections: list[str] = []
    for header in LANE_ORDER:
        lines = [block.line for block in kept if block.lane == header]
        if lines:
            sections.append(f"{header}:\n" + "\n".join(lines))
    return "\n\n".join(sections)


async def assemble_context_pack(
    query: str,
    principal_id: uuid.UUID | None = None,
    token_budget: int | None = None,
    k: int = 8,
    scope: uuid.UUID | None = None,
) -> str:
    """Recall for a query and pack the fused lanes into a token-budgeted, prompt-ready context.

    Reuses recall under its own acting_as so the pack rides the same fused facts, profiles,
    community and RAPTOR summaries, and working items a recall surfaces, then fits them to the
    budget. The one prompt-ready assembly an agent reads without choosing the lane mix itself.

    query: what to assemble context about.
    principal_id: identity whose row level security visibility scopes the recall, the system
        principal when null.
    token_budget: the token ceiling, the configured default when null.
    k: how many hits and seed facts the underlying recall surfaces.
    scope: group id narrowing the read to that group's composed graph, the whole visible union
        when null.
    """
    principal_id = principal_id or settings.system_principal_id
    result = await recall(query, principal_id=principal_id, k=k, scope=scope)
    return pack_context(result, token_budget or settings.context_token_budget)
