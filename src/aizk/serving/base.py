import asyncio
from collections.abc import Callable, Iterable
from functools import cache

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.openai import OpenAIProvider


@cache
def openai_client(url: str, api_key: str, timeout: float) -> AsyncOpenAI:
    """Reuse one OpenAI-compatible client per endpoint configuration."""
    return AsyncOpenAI(base_url=url, api_key=api_key or "local", timeout=timeout)


@cache
def llm_model(url: str, api_key: str, name: str) -> Model:
    """Reuse one provider-neutral structured generation model per endpoint."""
    return OpenAIChatModel(
        name,
        provider=OpenAIProvider(base_url=url, api_key=api_key or "local"),
        profile=ModelProfile(
            supports_json_schema_output=True,
            default_structured_output_mode="native",
        ),
    )


@cache
def http_client(url: str, api_key: str, timeout: float) -> httpx.AsyncClient:
    """Reuse one JSON HTTP client per endpoint configuration."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.AsyncClient(
        base_url=f"{url.rstrip('/')}/",
        headers=headers,
        timeout=timeout,
    )


@cache
def request_throttle(url: str, concurrency: int) -> asyncio.Semaphore:
    """Share one concurrency limit across every client using a model endpoint."""
    return asyncio.Semaphore(concurrency)


class OpenAIService:
    """Shared endpoint state for an OpenAI-compatible model service."""

    __slots__ = ("client", "model")

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self.client = client
        self.model = model


def ordered_results[ResultT](
    rows: Iterable[ResultT],
    expected: int,
    service: str,
    index: Callable[[ResultT], int],
) -> list[ResultT]:
    """Restore input order and reject missing, duplicate, or out-of-range result indexes."""
    ordered = sorted(rows, key=index)
    indexes = [index(row) for row in ordered]
    if indexes != list(range(expected)):
        raise ValueError(
            f"{service} returned invalid result indexes {indexes} for {expected} inputs"
        )
    return ordered


class HttpService:
    """Shared typed request path for JSON model sidecars."""

    __slots__ = ("client", "throttle")

    def __init__(self, client: httpx.AsyncClient, throttle: asyncio.Semaphore) -> None:
        self.client = client
        self.throttle = throttle

    async def post[ResponseT: BaseModel](
        self,
        route: str,
        request: BaseModel,
        response: type[ResponseT],
    ) -> ResponseT:
        """Post one model request and validate its successful response."""
        async with self.throttle:
            reply = await self.client.post(route, json=request.model_dump())
        reply.raise_for_status()
        return response.model_validate(reply.json())
