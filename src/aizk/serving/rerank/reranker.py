from functools import cache

import httpx

from ...config import settings


@cache
def client() -> httpx.AsyncClient:
    """Reuse the rerank HTTP client for the process lifetime."""
    headers = (
        {"Authorization": f"Bearer {settings.rerank_api_key}"} if settings.rerank_api_key else {}
    )
    return httpx.AsyncClient(
        base_url=f"{settings.rerank_url.rstrip('/')}/",
        headers=headers,
        timeout=settings.rerank_request_timeout,
    )


def templated(query: str, texts: list[str]) -> tuple[str, list[str]]:
    """Wrap the query and documents in the configured cross-encoder prompt scaffold."""
    wrapped_query = (
        settings.rerank_query_template.format(instruction=settings.rerank_instruction, query=query)
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


async def rerank(query: str, texts: list[str]) -> list[float]:
    """Score texts against a query through the cross-encoder rerank endpoint.

    query: the question the evidence is scored against.
    texts: candidate evidence lines, scores return aligned to their order.
    """
    if not texts:
        return []
    wrapped_query, wrapped_texts = templated(query, texts)
    response = await client().post(
        "rerank",
        json={"model": settings.rerank_model, "query": wrapped_query, "documents": wrapped_texts},
    )
    response.raise_for_status()
    results = response.json()["results"]
    if len(results) != len(texts):
        raise ValueError(f"reranker returned {len(results)} scores for {len(texts)} texts")
    scores = [0.0] * len(texts)
    for result in results:
        scores[result["index"]] = float(result["relevance_score"])
    return scores
