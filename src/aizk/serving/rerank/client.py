import asyncio

import httpx

from ...config import settings
from ..base import HttpService, http_client, ordered_results, request_throttle
from .models import RerankRequest, RerankResponse


class RerankClient(HttpService):
    """Cross-encoder scoring through the configured rerank service."""

    __slots__ = ("model",)

    def __init__(
        self,
        client: httpx.AsyncClient,
        model: str,
        throttle: asyncio.Semaphore,
    ) -> None:
        super().__init__(client, throttle)
        self.model = model

    @classmethod
    def configured(cls) -> RerankClient:
        """Build the service from the live reranker settings."""
        return cls(
            http_client(
                settings.rerank_url,
                settings.rerank_api_key,
                settings.rerank_request_timeout,
            ),
            settings.rerank_model,
            request_throttle(settings.rerank_url, settings.rerank_concurrency),
        )

    @staticmethod
    def templated(query: str, texts: list[str]) -> tuple[str, list[str]]:
        """Wrap a query and documents in the cross-encoder prompt scaffold."""
        wrapped_query = (
            settings.rerank_query_template.format(
                instruction=settings.rerank_instruction,
                query=query,
            )
            if settings.rerank_query_template
            else query
        )
        wrapped_texts = [
            settings.rerank_document_template.format(document=text)
            if settings.rerank_document_template
            else text
            for text in texts
        ]
        return wrapped_query, wrapped_texts

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        """Score texts against a query and restore their input order."""
        if not texts:
            return []
        wrapped_query, wrapped_texts = self.templated(query, texts)
        response = await self.post(
            "rerank",
            RerankRequest(model=self.model, query=wrapped_query, documents=wrapped_texts),
            RerankResponse,
        )
        if len(response.results) != len(texts):
            raise ValueError(
                f"reranker returned {len(response.results)} scores for {len(texts)} texts"
            )
        return [
            result.relevance_score
            for result in ordered_results(
                response.results,
                len(texts),
                "reranker",
                lambda row: row.index,
            )
        ]


async def rerank(query: str, texts: list[str]) -> list[float]:
    """Score texts through the configured cross-encoder."""
    return await RerankClient.configured().rerank(query, texts)
