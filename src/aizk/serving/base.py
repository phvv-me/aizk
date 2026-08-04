import asyncio
from collections.abc import Callable, Iterable, Mapping
from functools import cache
from typing import Literal, Protocol, runtime_checkable

import httpx
from loguru import logger
from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse
from patos import FrozenFlexModel
from pydantic import BaseModel, JsonValue
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

# Every network client constructed by the factories below, so the composition root can
# close each exactly once at shutdown through `close_clients`.
_open_clients: list[AsyncOpenAI | httpx.AsyncClient] = []


class EmbeddingsResource(Protocol):
    """The one embeddings call the embedding service issues."""

    async def create(
        self,
        *,
        model: str,
        input: list[str],
        dimensions: int,
        encoding_format: Literal["float"],
        extra_body: Mapping[str, JsonValue] | None,
    ) -> CreateEmbeddingResponse: ...


@runtime_checkable
class OpenAIBackend(Protocol):
    """The OpenAI-compatible client surface the embedding service consumes.

    Typed as the used surface so a recording double validates in place of the real
    `AsyncOpenAI` client without weakening field validation.
    """

    @property
    def embeddings(self) -> EmbeddingsResource: ...

    async def post[ResponseT](
        self,
        path: str,
        *,
        cast_to: type[ResponseT],
        body: Mapping[str, JsonValue],
    ) -> ResponseT: ...


@cache
def openai_client(
    url: str,
    api_key: str,
    timeout: float,
    headers: tuple[tuple[str, str], ...] = (),
) -> AsyncOpenAI:
    """Intern one OpenAI-compatible client per endpoint configuration."""
    client = AsyncOpenAI(
        base_url=url,
        api_key=api_key or "local",
        timeout=timeout,
        default_headers=dict(headers),
    )
    _open_clients.append(client)
    return client


@cache
def llm_model(
    url: str,
    api_key: str,
    name: str,
    timeout: float,
    headers: tuple[tuple[str, str], ...] = (),
) -> Model:
    """Intern one provider-neutral structured generation model per endpoint."""
    return OpenAIChatModel(
        name,
        provider=OpenAIProvider(openai_client=openai_client(url, api_key, timeout, headers)),
        profile=ModelProfile(
            supports_json_schema_output=True,
            default_structured_output_mode="native",
        ),
    )


@cache
def http_client(
    url: str,
    api_key: str,
    timeout: float,
    headers: tuple[tuple[str, str], ...] = (),
) -> httpx.AsyncClient:
    """Intern one JSON HTTP client per endpoint configuration."""
    default_headers = dict(headers)
    if api_key:
        default_headers["Authorization"] = f"Bearer {api_key}"
    client = httpx.AsyncClient(
        base_url=f"{url.rstrip('/')}/",
        headers=default_headers,
        timeout=timeout,
    )
    _open_clients.append(client)
    return client


@cache
def request_throttle(url: str, concurrency: int) -> asyncio.Semaphore:
    """Share one concurrency limit across every client using a model endpoint."""
    return asyncio.Semaphore(concurrency)


async def close_clients() -> None:
    """Close every interned endpoint client once and reset the interning.

    The clients are event-loop bound, so the runtime that assembled them closes them at
    the end of its lifetime and the cleared factories rebuild fresh clients for the next
    assembled runtime. A client whose transport was first used under an earlier,
    already-closed loop cannot close cleanly, so it is dropped instead of leaking the
    shutdown.
    """
    while _open_clients:
        client = _open_clients.pop()
        try:
            if isinstance(client, httpx.AsyncClient):
                await client.aclose()
            else:
                await client.close()
        except RuntimeError:
            logger.warning("dropped a serving client bound to an already-closed event loop")
    for factory in (openai_client, llm_model, http_client, request_throttle):
        factory.cache_clear()


class OpenAIService(FrozenFlexModel):
    """Shared endpoint state for an OpenAI-compatible model service."""

    client: OpenAIBackend
    model: str


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


class HttpService(FrozenFlexModel):
    """Shared typed request path for JSON model sidecars."""

    client: httpx.AsyncClient
    throttle: asyncio.Semaphore

    async def post[ResponseT: BaseModel](
        self,
        route: str,
        request: BaseModel | Mapping[str, JsonValue],
        response: type[ResponseT],
    ) -> ResponseT:
        """Post one model request and validate its successful response."""
        payload = request.model_dump() if isinstance(request, BaseModel) else dict(request)
        async with self.throttle:
            reply = await self.client.post(route, json=payload)
        reply.raise_for_status()
        return response.model_validate(reply.json())
