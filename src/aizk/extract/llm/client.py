import functools

from openai import AsyncOpenAI

from ...config import settings


@functools.cache
def build_client(llm_url: str, llm_model: str, llm_api_key: str) -> AsyncOpenAI:
    """Build an AsyncOpenAI client for an endpoint, memoized so its httpx pool is reused.

    The underlying openai client owns an httpx connection pool that should be shared rather than
    rebuilt per call, so the result is cached on the endpoint. `llm_model` and `llm_api_key` key
    the cache too, so two models on one endpoint or a rotated cloud key each get a distinct
    client, with an empty key falling back to the `ollama` placeholder a local server ignores.

    llm_url: base URL of the OpenAI-compatible chat endpoint.
    llm_model: chat model id served at the endpoint, part of the cache key.
    llm_api_key: bearer token for the endpoint, empty for a local server that ignores it.
    """
    return AsyncOpenAI(
        base_url=llm_url,
        api_key=llm_api_key or "ollama",
        timeout=settings.llm_request_timeout,
    )


def client_for(llm_url: str, llm_model: str, llm_api_key: str) -> AsyncOpenAI:
    """Return a process-cached AsyncOpenAI client bound to the given chat endpoint.

    `structured` calls this client's native `chat.completions.parse` under grammar-constrained
    decoding, so the response already validates against the schema with no retry-until-valid layer
    needed. The caller typically resolves the endpoint through `provider_settings()`'s overlay of a
    named provider onto the configured fields.

    llm_url: base URL of the OpenAI-compatible chat endpoint.
    llm_model: chat model id served at the endpoint.
    llm_api_key: bearer token for the endpoint, empty for a local server that ignores it.
    """
    return build_client(llm_url, llm_model, llm_api_key)
