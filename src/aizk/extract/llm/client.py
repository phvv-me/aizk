from openai import AsyncOpenAI
from patos import Singleton

from ...config import settings


class LLMClientPool(Singleton):
    """The single pool of endpoint-keyed AsyncOpenAI chat clients, reused across calls.

    Every `structured` call resolves its own endpoint through `provider_settings()`, since
    `settings.llm_provider` may name a different provider from one call to the next, so this pools
    a client per (url, model, key) tuple rather than fixing one client at construction the way
    `Embedder`/`Reranker` do. A `patos` singleton, so the pool itself, and each pooled client's own
    httpx connection pool, are shared for the process's lifetime rather than rebuilt per call, the
    same reuse `functools.cache` gave the free-function version this replaces.

    clients: the pooled clients, keyed by the endpoint tuple that built them.
    """

    def __init__(self) -> None:
        self.clients: dict[tuple[str, str, str], AsyncOpenAI] = {}

    def client_for(self, llm_url: str, llm_model: str, llm_api_key: str) -> AsyncOpenAI:
        """Return the pooled client for an endpoint, building and caching one on first use.

        `llm_model` and `llm_api_key` join `llm_url` in the cache key, so two models on one
        endpoint or a rotated cloud key each get a distinct client, with an empty key falling back
        to the `ollama` placeholder a local server ignores.

        llm_url: base URL of the OpenAI-compatible chat endpoint.
        llm_model: chat model id served at the endpoint, part of the cache key.
        llm_api_key: bearer token for the endpoint, empty for a local server that ignores it.
        """
        key = (llm_url, llm_model, llm_api_key)
        if key not in self.clients:
            self.clients[key] = AsyncOpenAI(
                base_url=llm_url,
                api_key=llm_api_key or "ollama",
                timeout=settings.llm_request_timeout,
            )
        return self.clients[key]
