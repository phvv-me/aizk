from collections.abc import Sequence
from math import ceil

from ..models import Candidate


def pack(
    candidates: Sequence[Candidate], budget: int, chars_per_token: float
) -> tuple[tuple[Candidate, ...], int]:
    """Cut the rescored candidates to the longest prefix whose cost fits the budget.

    candidates: the rescored cut in packing order.
    budget: the token budget the kept lines must fit inside.
    chars_per_token: the pricing heuristic, from settings at pack time.

    Each candidate costs its line plus one separator, and the walk stops at the first
    candidate that no longer fits, so packing order alone decides what survives.
    """
    kept: list[Candidate] = []
    used = 0
    for candidate in candidates:
        cost = tokens(candidate.line, chars_per_token) + 1
        if used + cost > budget:
            break
        used += cost
        kept.append(candidate)
    return tuple(kept), used


def tokens(text: str, chars_per_token: float) -> int:
    """Price text by the chars-per-token heuristic, deliberately tokenizer-free since
    the serving model's tokenizer is not available to the store."""
    return ceil(len(text) / chars_per_token)
