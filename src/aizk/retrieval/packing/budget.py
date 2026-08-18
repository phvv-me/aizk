from collections.abc import Sequence

from pydantic import UUID7

from ...provenance import Stance
from ..models import Candidate, Lane


def deduplicate(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Drop a source excerpt whose span settled, better-ranked evidence already speaks for.

    Only the excerpt side is ever dropped, and that asymmetry is the whole rule. A fact is
    one distinct statement, and a span commonly yields several, so dropping facts to remove
    redundancy would discard knowledge rather than repetition. An excerpt is the raw text
    those facts were distilled from, so it genuinely repeats a fact ranked above it.

    An unsettled fact is the exception, and it is the one that matters. A hedged, disputed
    or refuted claim reads as a clean assertion while the sentence behind it did not, so
    the excerpt carrying that sentence is not repetition, it is the correction. Such a fact
    never speaks for its span and the excerpt under it travels with it, which is what makes
    "the source wins" a property of what find returns rather than advice to the reader.

    An excerpt that ranks above the facts from its span is kept, and so are they. It earned
    its place, and the redundancy it leaves costs a little budget where dropping statements
    behind it would cost knowledge, which is the worse trade of the two.

    The rule keys on the span, so evidence is never weighed against a different document,
    and a community or overview summary names no span because it is a synthesis rather than
    a copy, so it is never dropped at all.
    """
    represented: set[UUID7] = set()
    kept: list[Candidate] = []
    for candidate in candidates:
        chunk = candidate.source_chunk_id
        if chunk is None:
            kept.append(candidate)
            continue
        if candidate.lane is Lane.Kind.SOURCES and chunk in represented:
            continue
        if candidate.stance is Stance.settled:
            represented.add(chunk)
        kept.append(candidate)
    return kept


def pack(candidates: Sequence[Candidate], budget: int) -> list[Candidate]:
    """Fill the token budget in merit order, skipping evidence too large for the room left.

    The walk never reorders, so a higher-ranked item is always offered its place first and
    the result stays deterministic. What an oversized item no longer does is end the walk.
    A single long excerpt used to cut everything ranked behind it, so a handful of fat
    source spans could spend an entire budget while short, well-ranked evidence behind them
    was never considered. Now such an item is stepped over and the walk continues.

    A budget too small for even the best item returns that item trimmed rather than nothing
    at all, because an answer grounded in a shortened excerpt beats an answer grounded in no
    evidence, and the trim carries a marker so a reader can see the text was cut.
    """
    kept: list[Candidate] = []
    remaining = budget
    for candidate in candidates:
        if (cost := candidate.token_count + 1) <= remaining:
            kept.append(candidate)
            remaining -= cost
    if kept or not candidates:
        return kept
    return [candidates[0].trimmed(budget)]
