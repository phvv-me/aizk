from functools import cache

from openai import AsyncOpenAI

from ...config import settings


@cache
def client_for(llm_url: str, llm_model: str, llm_api_key: str) -> AsyncOpenAI:
    """Return one cached chat client for an endpoint and credential tuple."""
    del llm_model
    return AsyncOpenAI(
        base_url=llm_url,
        api_key=llm_api_key or "ollama",
        timeout=settings.llm_request_timeout,
    )
