from collections.abc import Mapping
from typing import cast

from openai import AsyncOpenAI, BaseModel, Omit
from patos import Singleton

from ...config import settings


class RerankResult(BaseModel):
    """One scored candidate in a /v1/rerank response, in the Cohere/Jina result shape.

    Subclasses the OpenAI SDK's own `BaseModel` rather than the house `patos.FrozenModel`, since
    the low-level `client.post(cast_to=...)` escape hatch this reranker uses for the non-standard
    /v1/rerank endpoint only parses into its own model type.

    index: position of the candidate in the request's `documents` array.
    relevance_score: the cross-encoder's score for that candidate against the query.
    """

    index: int
    relevance_score: float


class RerankResponse(BaseModel):
    """The /v1/rerank response body, an unordered array of scored results.

    results: one entry per candidate, in the endpoint's own ranked order rather than input order.
    """

    results: list[RerankResult]


class Reranker(Singleton):
    """The single reranker, an OpenAI-compatible /v1/rerank client in the Cohere/Jina shape.

    Posts the query and candidate documents to the co-resident vLLM reranker container through the
    OpenAI SDK client's low-level `post`, since the non-standard /v1/rerank endpoint needs it. The
    endpoint answers with results in its own ranked order, which this realigns to the input order
    by the returned index so scores stay row-aligned with the candidates passed in. A `patos`
    singleton, one shared instance built the first time anything constructs a `Reranker()`.

    rerank_url: base URL of the OpenAI-compatible endpoint, ending at the /v1 prefix.
    rerank_model: served model name the endpoint matches, the vllm-rerank --served-model-name.
    """

    def __init__(self) -> None:
        self.rerank_url = settings.rerank_url
        self.rerank_model = settings.rerank_model
        self.api_key = settings.rerank_api_key
        drop_auth: Mapping[str, str] = cast("Mapping[str, str]", {"Authorization": Omit()})
        self.client = AsyncOpenAI(
            base_url=self.rerank_url,
            api_key=self.api_key or "none",
            timeout=settings.rerank_request_timeout,
            default_headers=None if self.api_key else drop_auth,
        )

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        """Score candidates against the query through the /v1/rerank endpoint.

        query: the search string.
        candidates: candidate texts row-aligned with the returned scores.
        """
        if not candidates:
            return []
        response = await self.client.post(
            "/rerank",
            cast_to=RerankResponse,
            body={"model": self.rerank_model, "query": query, "documents": candidates},
        )
        scores = [0.0] * len(candidates)
        for result in response.results:
            scores[result.index] = result.relevance_score
        return scores
