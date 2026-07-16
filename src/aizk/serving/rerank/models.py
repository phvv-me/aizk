from patos import FrozenModel


class RerankRequest(FrozenModel):
    """One cross-encoder ranking request."""

    model: str
    query: str
    documents: list[str]


class RerankResult(FrozenModel):
    """One document score returned by the cross-encoder."""

    index: int
    relevance_score: float


class RerankResponse(FrozenModel):
    """The scored rows returned by the cross-encoder."""

    results: list[RerankResult]
