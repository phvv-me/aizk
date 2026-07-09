import asyncio
from typing import cast

from openai import AsyncOpenAI

from aizk.serving.rerank.reranker import Reranker, RerankResponse, RerankResult


class FakeClient:
    """A /v1/rerank client stand-in returning canned results, one index out of range."""

    async def post(self, path: str, *, cast_to: type, body: dict) -> RerankResponse:
        """Return ranked results in the endpoint's own order, one bogus index the guard skips."""
        return RerankResponse(
            results=[
                RerankResult(index=1, relevance_score=0.9),
                RerankResult(index=0, relevance_score=0.3),
                RerankResult(index=5, relevance_score=0.7),  # out of range for two candidates
            ]
        )


def test_rerank_realigns_by_index_and_skips_an_out_of_range_result() -> None:
    """Scores realign to input order by the returned index, an out-of-range index is skipped rather
    than raising IndexError, and no candidates short-circuits to an empty list."""
    reranker = Reranker.__new__(Reranker)
    reranker.rerank_model = "test-reranker"
    reranker.client = cast(AsyncOpenAI, FakeClient())
    assert asyncio.run(reranker.rerank("query", ["a", "b"])) == [0.3, 0.9]
    assert asyncio.run(reranker.rerank("query", [])) == []
