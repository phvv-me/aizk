from typing import Literal

from patos import FrozenModel


class RerankRequest(FrozenModel):
    """One cross-encoder ranking request."""

    model: str
    query: str
    documents: list[str]
    max_tokens_per_query: int | None = None
    max_tokens_per_doc: int | None = None
    truncate_prompt_tokens: int | None = None
    truncation_side: Literal["left", "right"] | None = None


class RerankResult(FrozenModel):
    """One document score returned by the cross-encoder."""

    index: int
    relevance_score: float


class RerankResponse(FrozenModel):
    """The scored rows returned by the cross-encoder."""

    results: list[RerankResult]
