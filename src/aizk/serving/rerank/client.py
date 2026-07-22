from pydantic import JsonValue

from ...config import Settings
from ..base import HttpService, http_client, ordered_results, request_throttle
from .models import RerankRequest, RerankResponse


class RerankClient(HttpService):
    """Cross-encoder scoring through the configured rerank service."""

    model: str
    instruction: str
    query_template: str
    document_template: str
    query_max_tokens: int
    document_max_tokens: int
    enabled: bool
    request_format: str
    extra_body: dict[str, JsonValue] = {}

    @classmethod
    def from_settings(cls, config: Settings) -> RerankClient:
        """Build the service from explicit reranker settings."""
        return cls(
            client=http_client(
                config.rerank_url,
                config.rerank_api_key,
                config.rerank_request_timeout,
                tuple(
                    sorted(
                        (name, value.get_secret_value())
                        for name, value in config.rerank_headers.items()
                    )
                ),
            ),
            model=config.rerank_model,
            throttle=request_throttle(config.rerank_url, config.rerank_concurrency),
            instruction=config.rerank_instruction,
            query_template=config.rerank_query_template,
            document_template=config.rerank_document_template,
            query_max_tokens=config.rerank_query_max_tokens,
            document_max_tokens=config.rerank_document_max_tokens,
            enabled=config.rerank_enabled,
            request_format=config.rerank_request_format,
            extra_body=config.rerank_extra_body,
        )

    def templated(self, query: str, texts: list[str]) -> tuple[str, list[str]]:
        """Wrap a query and documents in the cross-encoder prompt scaffold."""
        wrapped_query = (
            self.query_template.format(instruction=self.instruction, query=query)
            if self.query_template
            else query
        )
        wrapped_texts = [
            self.document_template.format(document=text) if self.document_template else text
            for text in texts
        ]
        return wrapped_query, wrapped_texts

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        """Score texts against a query and restore their input order."""
        if not texts:
            return []
        if not self.enabled:
            return [1.0 / (index + 1) for index in range(len(texts))]
        wrapped_query, wrapped_texts = self.templated(query, texts)
        local = self.request_format == "vllm"
        request = RerankRequest(
            model=self.model,
            query=wrapped_query,
            documents=wrapped_texts,
            max_tokens_per_query=self.query_max_tokens if local else None,
            max_tokens_per_doc=self.document_max_tokens if local else None,
            truncate_prompt_tokens=-1 if local else None,
            truncation_side="left" if local else None,
        )
        response = await self.post(
            "rerank",
            request.model_dump(exclude_none=True) | self.extra_body,
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
