from datetime import datetime

from patos import FrozenModel


class RecallContext(FrozenModel):
    """The per-call inputs every recall lane reads, bound once and threaded through `gather_lanes`.

    query: natural-language search string, also the lexical and rerank text.
    vector: dense query embedding, already resolved before any lane runs.
    k: number of fused hits and of seed facts to surface.
    as_of: world-time the graph facts must be valid at, the live graph when null.
    thematic: whether the query reads as a broad, global-view question, the community and RAPTOR
        lanes' own gate.
    ppr_on: whether the multi-hop personalized-pagerank lane widens the seed and neighbor facts.
    raptor_on: whether the RAPTOR summary-tree lane runs at all.
    """

    query: str
    vector: list[float]
    k: int
    as_of: datetime | None
    thematic: bool
    ppr_on: bool
    raptor_on: bool
