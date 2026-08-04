import abc
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import cache
from typing import ClassVar, Protocol, Self, runtime_checkable

import httpx
from loguru import logger
from patos import FrozenFlexModel, Registry
from pydantic import BaseModel, Field, JsonValue
from pydantic.networks import AnyHttpUrl
from pydantic.types import SecretStr

from ...config import Settings
from ..docling import (
    ArtifactBytes,
    DoclingConversionError,
    DoclingOutput,
    DoclingResponse,
    UnsafeArtifactError,
    URISource,
)
from .models import (
    Freshness,
    ProviderTraffic,
    ProviderUnavailable,
    SearchLane,
    WebPage,
    WebResult,
)

# Every client the provider factories intern, closed once by the composition root.
_open_clients: list[httpx.AsyncClient] = []


@cache
def web_client(
    url: str, timeout: float, headers: tuple[tuple[str, str], ...]
) -> httpx.AsyncClient:
    """Intern one JSON HTTP client per external endpoint configuration."""
    client = httpx.AsyncClient(
        base_url=f"{url.rstrip('/')}/", headers=dict(headers), timeout=timeout
    )
    _open_clients.append(client)
    return client


async def close_web_clients() -> None:
    """Close every interned external client once and reset the interning."""
    while _open_clients:
        await _open_clients.pop().aclose()
    web_client.cache_clear()


class FirecrawlHit(BaseModel):
    """One row of a Firecrawl search response."""

    url: AnyHttpUrl
    title: str | None = None
    description: str | None = None


class FirecrawlSearch(BaseModel):
    """The web section of a Firecrawl search response."""

    data: dict[str, list[FirecrawlHit]] = Field(default_factory=dict)


class FirecrawlMetadata(BaseModel):
    """The page metadata Firecrawl reports beside a scrape."""

    title: str | None = None


class FirecrawlDocument(BaseModel):
    """One scraped page as Firecrawl returns it."""

    markdown: str | None = None
    metadata: FirecrawlMetadata = Field(default_factory=FirecrawlMetadata)


class FirecrawlScrape(BaseModel):
    """A Firecrawl scrape response envelope."""

    data: FirecrawlDocument = Field(default_factory=FirecrawlDocument)


class ExaHit(BaseModel):
    """One row of an Exa search response, with the inline contents Exa returns."""

    url: AnyHttpUrl
    title: str | None = None
    text: str | None = None
    published_at: datetime | None = Field(default=None, alias="publishedDate")


class ExaSearch(BaseModel):
    """An Exa search response envelope."""

    results: list[ExaHit] = Field(default_factory=list)


class JinaHit(BaseModel):
    """One row of a Jina reader search response."""

    url: AnyHttpUrl
    title: str | None = None
    description: str | None = None
    content: str | None = None


class JinaSearch(BaseModel):
    """A Jina search response envelope."""

    data: list[JinaHit] = Field(default_factory=list)


class JinaRead(BaseModel):
    """A Jina reader response envelope."""

    data: JinaHit


async def fetch_json[ResponseT: BaseModel](
    client: httpx.AsyncClient,
    method: str,
    route: str,
    response: type[ResponseT],
    provider: str,
    traffic: ProviderTraffic,
    json: Mapping[str, JsonValue] | None = None,
) -> ResponseT:
    """One external request whose every failure mode becomes `ProviderUnavailable`.

    Transport errors, refusals, rate limits, and unparsable bodies are all the same event
    to a lane chain, which is that this provider did not answer, so they arrive as one
    exception the chain skips on rather than four the caller would have to distinguish.

    Only the failure's shape is ever logged, never its text. A validation error renders the
    offending response inline, and a provider's response carries page content and licence
    text, so logging the exception itself would copy a third party's body into the operator
    log for every malformed reply. The status code and the exception type say everything an
    operator can act on.
    """
    try:
        reply = await client.request(method, route, json=json)
        traffic.record(len(reply.request.content), len(reply.content))
        reply.raise_for_status()
        return response.model_validate(reply.json())
    except httpx.HTTPStatusError as refused:
        logger.warning(
            "web provider {} refused with status {}", provider, refused.response.status_code
        )
        raise ProviderUnavailable(f"{provider} did not answer") from None
    except (httpx.HTTPError, ValueError) as refused:
        logger.warning("web provider {} did not answer, {}", provider, type(refused).__name__)
        raise ProviderUnavailable(f"{provider} did not answer") from None


class WebSearcher(Registry, FrozenFlexModel, abc.ABC):
    """One external search lane behind a uniform, licence-aware contract.

    Every implementation answers the same question the same way and declares two things
    the rest of the engine relies on. `spend` is what one call costs in provider credits,
    which the quota ledger charges before anything leaves the machine. `persistable` is
    whether this provider's terms allow keeping what it returns, and the cache refuses
    anything a provider marked unpersistable produced, so the licence is carried by the
    object rather than remembered by whoever writes the store.
    """

    lane: ClassVar[SearchLane]

    client: httpx.AsyncClient
    api_key: SecretStr
    traffic: ProviderTraffic = Field(default_factory=ProviderTraffic)

    @property
    def spend(self) -> int:
        """Provider credits one search call charges against the web quota ledger."""
        return 1

    @property
    def persistable(self) -> bool:
        """Whether this provider's terms permit storing what it returns."""
        return False

    @classmethod
    @abc.abstractmethod
    def from_settings(cls, config: Settings) -> Self | None:
        """Build this provider, or nothing at all when the deployment left its key unset."""

    @abc.abstractmethod
    async def search(self, query: str, limit: int) -> tuple[WebResult, ...]:
        """This provider's hits for one already sanitized query."""


class Firecrawl(WebSearcher):
    """Firecrawl search, the keyword lane.

    Firecrawl's terms cover storing the pages it retrieves for the account that retrieved
    them, so pages it reads may be cached.
    """

    lane: ClassVar[SearchLane] = SearchLane.keyword

    @property
    def persistable(self) -> bool:
        """Firecrawl licences the retrieved page to the retrieving account."""
        return True

    @classmethod
    def from_settings(cls, config: Settings) -> Self | None:
        """Build the Firecrawl search client when the deployment configured its key."""
        key = config.web_firecrawl_api_key.get_secret_value()
        if not key:
            return None
        return cls(
            client=web_client(
                str(config.web_firecrawl_url),
                config.web_search_timeout,
                (("Authorization", f"Bearer {key}"),),
            ),
            api_key=config.web_firecrawl_api_key,
        )

    async def search(self, query: str, limit: int) -> tuple[WebResult, ...]:
        """Firecrawl's web hits for one sanitized query."""
        found = await fetch_json(
            self.client,
            "POST",
            "v2/search",
            FirecrawlSearch,
            self.name,
            self.traffic,
            json={"query": query, "limit": limit, "sources": ["web"]},
        )
        return tuple(
            WebResult(url=hit.url, title=hit.title, snippet=hit.description)
            for hit in found.data.get("web", [])
        )


class Exa(WebSearcher):
    """Exa neural search, the semantic lane.

    Exa returns page contents inline with its hits, which makes it one call rather than a
    search followed by a fetch. Its terms do not licence redistribution or storage of that
    text, so nothing Exa returns is ever written to the store. Its rows live for exactly
    the request that asked for them.
    """

    lane: ClassVar[SearchLane] = SearchLane.semantic

    category: str | None = None

    @classmethod
    def from_settings(cls, config: Settings) -> Self | None:
        """Build the Exa search client when the deployment configured its key."""
        key = config.web_exa_api_key.get_secret_value()
        if not key:
            return None
        return cls(
            client=web_client(
                str(config.web_exa_url),
                config.web_search_timeout,
                (("x-api-key", key),),
            ),
            api_key=config.web_exa_api_key,
        )

    async def search(self, query: str, limit: int) -> tuple[WebResult, ...]:
        """Exa's hits and their inline contents for one sanitized query."""
        body: dict[str, JsonValue] = {
            "query": query,
            "numResults": limit,
            "contents": {"text": True},
        }
        if self.category is not None:
            body["category"] = self.category
        found = await fetch_json(
            self.client, "POST", "search", ExaSearch, self.name, self.traffic, json=body
        )
        return tuple(
            WebResult(
                url=hit.url,
                title=hit.title,
                snippet=hit.text,
                published_at=hit.published_at,
            )
            for hit in found.results
        )


class Jina(WebSearcher):
    """Jina search, registered as a keyword alternative and unconfigured by default.

    Jina reads the live page for every hit, so what it returns is the page itself and may
    be cached. It stays out of the default chain because a deployment that wants it must
    choose it deliberately in `web_search_keyword_providers`.
    """

    lane: ClassVar[SearchLane] = SearchLane.keyword

    @property
    def persistable(self) -> bool:
        """Jina returns the live page it read, which the retrieving account may keep."""
        return True

    @classmethod
    def from_settings(cls, config: Settings) -> Self | None:
        """Build the Jina search client when the deployment configured its key."""
        key = config.web_jina_api_key.get_secret_value()
        if not key:
            return None
        return cls(
            client=web_client(
                str(config.web_jina_search_url),
                config.web_search_timeout,
                (("Authorization", f"Bearer {key}"), ("Accept", "application/json")),
            ),
            api_key=config.web_jina_api_key,
        )

    async def search(self, query: str, limit: int) -> tuple[WebResult, ...]:
        """Jina's hits for one sanitized query, already carrying page text.

        The query travels in the request body rather than as a URL parameter. Jina accepts
        both, and a query string ends up in httpx's exception messages and in the
        `url.full` attribute of every outbound span, which would put the one text this
        whole feature works to sanitize into the trace exporter.
        """
        found = await fetch_json(
            self.client,
            "POST",
            "",
            JinaSearch,
            self.name,
            self.traffic,
            json={"q": query, "num": limit},
        )
        return tuple(
            WebResult(url=hit.url, title=hit.title, snippet=hit.content or hit.description)
            for hit in found.data[:limit]
        )


@runtime_checkable
class PageSource(Protocol):
    """The one bounded read the house fetcher makes, the same one artifact intake uses."""

    async def read_uri(self, source: URISource) -> ArtifactBytes: ...


@runtime_checkable
class PageConverter(Protocol):
    """The one conversion call the house fetcher makes."""

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse: ...


@runtime_checkable
class WebFetcher(Protocol):
    """One page reader, tried in chain order until a page comes back."""

    @property
    def name(self) -> str:
        """The chain key a deployment lists in `web_search_fetch_providers`."""
        ...

    @property
    def persistable(self) -> bool:
        """Whether pages this reader returns may be written to the store."""
        ...

    @property
    def spend(self) -> int:
        """Provider credits one fetch charges."""
        ...

    @property
    def traffic(self) -> ProviderTraffic:
        """The wire sizes of this reader's last call, for the ledger."""
        ...

    async def fetch(self, url: AnyHttpUrl, fresh: bool) -> WebPage:
        """Read one page as Markdown, raising `ProviderUnavailable` when it cannot."""
        ...


class FirecrawlReader(FrozenFlexModel):
    """Firecrawl scrape, the first reader in the fetch chain.

    `maxAge` is Firecrawl's own cache window, so a question the router called stable reuses
    Firecrawl's copy instead of paying for a live crawl, and a `fresh` call sends zero to
    force one.
    """

    client: httpx.AsyncClient
    max_age_seconds: int
    traffic: ProviderTraffic = Field(default_factory=ProviderTraffic)

    @property
    def name(self) -> str:
        """The chain key for this reader."""
        return "firecrawl-reader"

    @property
    def persistable(self) -> bool:
        """Firecrawl licences the retrieved page to the retrieving account."""
        return True

    @property
    def spend(self) -> int:
        """One fetched page is one provider credit."""
        return 1

    @classmethod
    def from_settings(cls, config: Settings, freshness: Freshness) -> Self | None:
        """Build the scrape client when the deployment configured a Firecrawl key."""
        key = config.web_firecrawl_api_key.get_secret_value()
        if not key:
            return None
        return cls(
            client=web_client(
                str(config.web_firecrawl_url),
                config.web_search_timeout,
                (("Authorization", f"Bearer {key}"),),
            ),
            max_age_seconds=cache_days(config, freshness) * 86_400,
        )

    async def fetch(self, url: AnyHttpUrl, fresh: bool) -> WebPage:
        """Scrape one page's main content as Markdown."""
        scraped = await fetch_json(
            self.client,
            "POST",
            "v2/scrape",
            FirecrawlScrape,
            self.name,
            self.traffic,
            json={
                "url": str(url),
                "formats": ["markdown"],
                "onlyMainContent": True,
                "maxAge": 0 if fresh else self.max_age_seconds * 1000,
            },
        )
        if not scraped.data.markdown:
            raise ProviderUnavailable("firecrawl returned no readable text")
        return WebPage(
            url=url,
            markdown=scraped.data.markdown,
            provider=self.name,
            retrieved_at=datetime.now(UTC),
            title=scraped.data.metadata.title,
            persistable=self.persistable,
        )


class DoclingReader(FrozenFlexModel):
    """The house reader, the fetch chain's fallback when no vendor scraper answers.

    It is the same bounded, SSRF-checked reader and the same Docling conversion that
    `remember(source_uri=...)` already runs, so a page reaching the cache through this
    path went through exactly the boundary a preserved source does.
    """

    reader: PageSource
    converter: PageConverter
    traffic: ProviderTraffic = Field(default_factory=ProviderTraffic)

    @property
    def name(self) -> str:
        """The chain key for this reader."""
        return "docling-reader"

    @property
    def persistable(self) -> bool:
        """The deployment fetched these bytes itself, so it may keep them."""
        return True

    @property
    def spend(self) -> int:
        """One fetched page is one provider credit."""
        return 1

    async def fetch(self, url: AnyHttpUrl, fresh: bool) -> WebPage:
        """Read one public page and convert it to Markdown through Docling.

        `fresh` has no effect here, because this reader holds no cache of its own and every
        call is already a live read.
        """
        del fresh
        try:
            artifact = await self.reader.read_uri(URISource(uri=url))
            converted = DoclingOutput.from_response(await self.converter.convert(artifact))
        except (
            DoclingConversionError,
            UnsafeArtifactError,
            httpx.HTTPError,
            ValueError,
        ) as refused:
            logger.warning("web provider {} did not answer: {}", self.name, refused)
            raise ProviderUnavailable("docling-reader could not read the page") from refused
        self.traffic.record(len(str(url).encode("utf-8")), len(artifact.content))
        return WebPage(
            url=url,
            markdown=converted.markdown,
            provider=self.name,
            retrieved_at=datetime.now(UTC),
            title=self.page_title(artifact),
            persistable=self.persistable,
        )

    @staticmethod
    def page_title(artifact: ArtifactBytes) -> str | None:
        """The filename the reader derived, used as the page title when it is not generic."""
        return None if artifact.filename == "artifact" else artifact.filename


def cache_days(config: Settings, freshness: Freshness) -> int:
    """How many days a page of one freshness stays usable before it must be read again."""
    return {
        Freshness.stable: config.web_search_stable_days,
        Freshness.dated: config.web_search_dated_days,
        Freshness.volatile: config.web_search_volatile_days,
    }[freshness]
