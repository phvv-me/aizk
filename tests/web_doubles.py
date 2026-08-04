from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import ClassVar, Self, cast

import httpx
from pydantic import Field
from pydantic.networks import AnyHttpUrl
from pydantic.types import SecretStr

from aizk.config import Settings
from aizk.integrations.clamav import CleanScan, ContentScanner
from aizk.integrations.docling import ArtifactBytes, DoclingResponse, URISource
from aizk.integrations.web import (
    ProviderTraffic,
    ProviderUnavailable,
    SearchLane,
    WebPage,
    WebResult,
    WebSearcher,
)
from aizk.serving.gate import MentionDetector
from aizk.usage import UsageCapture, UsageRecorder


def inert_client() -> httpx.AsyncClient:
    """A client whose transport refuses, so a double can never reach a real network."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a scripted provider must never open a connection")

    return httpx.AsyncClient(
        base_url="https://unused.test/", transport=httpx.MockTransport(refuse)
    )


class ScriptedSearcher(WebSearcher):
    """A search provider that answers from a script and never opens a socket."""

    lane: ClassVar[SearchLane] = SearchLane.keyword

    client: httpx.AsyncClient = Field(default_factory=inert_client)
    api_key: SecretStr = SecretStr("")
    results: tuple[WebResult, ...] = ()
    fails: bool = False
    calls: list[str] = []

    @classmethod
    def from_settings(cls, config: Settings) -> Self | None:
        """Never built from settings, since tests construct it with an explicit script."""
        del config
        return None

    async def search(self, query: str, limit: int) -> tuple[WebResult, ...]:
        """Record the sanitized query and answer from the script."""
        self.calls.append(query)
        self.traffic.record(len(query.encode("utf-8")), sum(len(r.preview) for r in self.results))
        if self.fails:
            raise ProviderUnavailable("scripted refusal")
        return self.results[:limit]


class ScriptedFetcher:
    """A page reader that answers from a script and never opens a socket."""

    def __init__(
        self,
        page: WebPage | None = None,
        name: str = "scripted-reader",
        persistable: bool = True,
    ) -> None:
        self.page = page
        self.reader_name = name
        self.licensed = persistable
        self.calls: list[tuple[str, bool]] = []
        self.measured = ProviderTraffic()

    @property
    def name(self) -> str:
        return self.reader_name

    @property
    def persistable(self) -> bool:
        return self.licensed

    @property
    def spend(self) -> int:
        return 1

    @property
    def traffic(self) -> ProviderTraffic:
        return self.measured

    async def fetch(self, url: AnyHttpUrl, fresh: bool) -> WebPage:
        self.calls.append((str(url), fresh))
        self.measured.record(
            len(str(url).encode("utf-8")), len(self.page.markdown) if self.page else 0
        )
        if self.page is None:
            raise ProviderUnavailable("scripted refusal")
        return self.page


class InertPageSource:
    """A page reader that refuses, so a chain-building test never reads anything."""

    async def read_uri(self, source: URISource) -> ArtifactBytes:
        del source
        raise AssertionError("an inert page source must never be read")


class InertPageConverter:
    """A converter that refuses, so a chain-building test never converts anything."""

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        del artifact
        raise AssertionError("an inert converter must never run")


class ScriptedGate:
    """A GLiNER gate double returning fixed mentions or refusing to answer."""

    def __init__(self, mentions: Sequence[str] = (), error: Exception | None = None) -> None:
        self.found = list(mentions)
        self.error = error
        self.calls: list[tuple[str, tuple[str, ...], float]] = []

    async def mentions(self, text: str, labels: Iterable[str], threshold: float) -> list[str]:
        self.calls.append((text, tuple(labels), threshold))
        if self.error is not None:
            raise self.error
        return list(self.found)


class ScriptedScanner:
    """A ClamAV double that answers clean or raises whatever a test hands it."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.scanned: list[bytes] = []

    async def scan(self, content: bytes) -> CleanScan:
        self.scanned.append(content)
        if self.error is not None:
            raise self.error
        return CleanScan(bytes_scanned=len(content))


class RecordingRecorder:
    """A usage recorder that keeps captures in memory instead of the durable queue."""

    def __init__(self) -> None:
        self.captures: list[UsageCapture] = []

    async def record(self, capture: UsageCapture) -> None:
        self.captures.append(capture)


def page(
    url: str = "https://example.test/page",
    markdown: str = "the public page",
    provider: str = "scripted-reader",
    persistable: bool = True,
) -> WebPage:
    """One fetched page, ready to hand a fetcher double."""
    return WebPage(
        url=cast("AnyHttpUrl", url),
        markdown=markdown,
        provider=provider,
        retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
        title="A public page",
        persistable=persistable,
    )


def hit(url: str = "https://example.test/page", snippet: str = "a snippet") -> WebResult:
    """One search row, ready to hand a searcher double."""
    return WebResult(url=cast("AnyHttpUrl", url), title="A public page", snippet=snippet)


def transport(
    handler: httpx.MockTransport, base: str = "https://provider.test"
) -> httpx.AsyncClient:
    """An HTTP client wired to an in-process transport, so no provider is ever dialed."""
    return httpx.AsyncClient(base_url=f"{base}/", transport=handler)


def as_gate(double: ScriptedGate) -> MentionDetector:
    """Present a gate double as the detection surface the sanitizer consumes."""
    return double


def as_scanner(double: ScriptedScanner) -> ContentScanner:
    """Present a scanner double as the scan surface the cache consumes."""
    return double


def as_recorder(double: RecordingRecorder) -> UsageRecorder:
    """Present a recorder double where the typed recorder is expected."""
    return cast("UsageRecorder", double)
