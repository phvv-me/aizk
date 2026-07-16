from collections.abc import Sequence
from itertools import accumulate

from ..models import Candidate


def pack(candidates: Sequence[Candidate], budget: int) -> list[Candidate]:
    """Cut candidates to the longest packing prefix within the token budget."""
    totals = accumulate(candidate.token_count + 1 for candidate in candidates)
    return [
        candidate for candidate, total in zip(candidates, totals, strict=True) if total <= budget
    ]
