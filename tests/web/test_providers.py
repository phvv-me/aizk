from datetime import UTC, datetime
from typing import cast

import dbutil
import httpx
import pytest
from pydantic.networks import AnyHttpUrl
from pydantic.types import SecretStr

from aizk.config import Settings, settings
from aizk.integrations.docling import (
    ArtifactBytes,
    DoclingConversionError,
    DoclingResponse,
    UnsafeArtifactError,
    URISource,
)
from aizk.integrations.docling.models import DoclingDocument
from aizk.integrations.web import (
    DoclingReader,
    Exa,
    Firecrawl,
    FirecrawlReader,
    Freshness,
    Jina,
    ProviderTraffic,
    ProviderUnavailable,
    SearchLane,
    WebPage,
    WebResult,
    WebSearcher,
    cache_days,
    close_web_clients,
)
from aizk.integrations.web.providers import fetch_json, web_client

_URL = "https://example.test/page"


def configured(**overrides: object) -> Settings:
    """Settings with every provider key filled unless a test blanks one out."""
    return settings.model_copy(
        update={
            "web_firecrawl_api_key": SecretStr("fc-key"),
            "web_exa_api_key": SecretStr("exa-key"),
            "web_jina_api_key": SecretStr("jina-key"),
            **overrides,
        }
    )


def responder(payload: dict[str, object], status: int = 200) -> httpx.MockTransport:
    """An in-process transport answering every request with one payload."""
    return httpx.MockTransport(lambda request: httpx.Response(status, json=payload))


def client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    """An HTTP client bound to an in-process transport, so no provider is ever dialed."""
    return httpx.AsyncClient(base_url="https://provider.test/", transport=transport)


def test_the_registry_answers_every_shipped_provider_name_with_its_lane() -> None:
    shipped = {"firecrawl", "exa", "jina"}
    names = set(WebSearcher.names())

    assert shipped <= names
    # Brave is absent on purpose, since its terms forbid this use, and so is SearXNG.
    assert not names & {"brave", "searxng"}
    assert WebSearcher.find("firecrawl") is Firecrawl
    assert Firecrawl.lane is SearchLane.keyword
    assert Exa.lane is SearchLane.semantic
    assert Jina.lane is SearchLane.keyword


@pytest.mark.parametrize(
    ("provider", "licensed"),
    [(Firecrawl, True), (Exa, False), (Jina, True)],
)
def test_only_providers_whose_terms_allow_storage_are_persistable(
    provider: type[WebSearcher], licensed: bool
) -> None:
    built = provider.from_settings(configured())

    assert built is not None
    assert built.persistable is licensed
    assert built.spend == 1


@pytest.mark.parametrize(
    ("provider", "blank"),
    [
        (Firecrawl, "web_firecrawl_api_key"),
        (Exa, "web_exa_api_key"),
        (Jina, "web_jina_api_key"),
    ],
)
def test_a_provider_without_its_key_refuses_to_be_built(
    provider: type[WebSearcher], blank: str
) -> None:
    assert provider.from_settings(configured(**{blank: SecretStr("")})) is None


def test_firecrawl_search_reads_the_web_section_of_its_response() -> None:
    provider = Firecrawl(
        client=client(
            responder(
                {
                    "success": True,
                    "data": {
                        "web": [
                            {"url": _URL, "title": "A page", "description": "a snippet"},
                            {"url": "https://example.test/other"},
                        ]
                    },
                }
            )
        ),
        api_key=SecretStr("fc-key"),
    )

    found = dbutil.run(provider.search("public question", 5))

    assert [str(item.url) for item in found] == [_URL, "https://example.test/other"]
    assert found[0].snippet == "a snippet"
    assert found[1].title is None


def test_exa_search_carries_its_inline_contents_and_publication_date() -> None:
    provider = Exa(
        client=client(
            responder(
                {
                    "results": [
                        {
                            "url": _URL,
                            "title": "A page",
                            "text": "the page body",
                            "publishedDate": "2026-01-02T00:00:00Z",
                        }
                    ]
                }
            )
        ),
        api_key=SecretStr("exa-key"),
        category="research paper",
    )

    (found,) = dbutil.run(provider.search("public question", 5))

    assert found.snippet == "the page body"
    assert found.published_at == datetime(2026, 1, 2, tzinfo=UTC)


def test_exa_sends_its_category_only_when_one_is_configured() -> None:
    bodies: list[object] = []

    def record(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read().decode())
        return httpx.Response(200, json={"results": []})

    plain = Exa(client=client(httpx.MockTransport(record)), api_key=SecretStr("exa-key"))
    categorised = plain.model_copy(update={"category": "news"})

    dbutil.run(plain.search("one", 3))
    dbutil.run(categorised.search("two", 3))

    assert "category" not in str(bodies[0])
    assert "news" in str(bodies[1])


def test_jina_prefers_page_content_over_the_result_description() -> None:
    provider = Jina(
        client=client(
            responder(
                {
                    "data": [
                        {"url": _URL, "title": "A", "description": "short", "content": "long"},
                        {"url": "https://example.test/b", "description": "only a description"},
                    ]
                }
            )
        ),
        api_key=SecretStr("jina-key"),
    )

    found = dbutil.run(provider.search("public question", 2))

    assert [item.snippet for item in found] == ["long", "only a description"]


@pytest.mark.parametrize(
    "transport",
    [
        httpx.MockTransport(lambda request: httpx.Response(429, json={})),
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json")),
    ],
    ids=["refused", "unreadable"],
)
def test_every_provider_failure_arrives_as_one_skippable_refusal(
    transport: httpx.MockTransport,
) -> None:
    provider = Firecrawl(client=client(transport), api_key=SecretStr("fc-key"))

    with pytest.raises(ProviderUnavailable):
        dbutil.run(provider.search("public question", 3))


def test_a_transport_error_is_a_refusal_rather_than_an_escape() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(ProviderUnavailable):
        dbutil.run(
            fetch_json(
                client(httpx.MockTransport(explode)),
                "GET",
                "anything",
                WebResult,
                "probe",
                ProviderTraffic(),
            )
        )


@pytest.mark.parametrize(("fresh", "expected"), [(False, 30 * 86_400_000), (True, 0)])
def test_the_firecrawl_reader_sends_its_cache_window_unless_the_call_is_fresh(
    fresh: bool, expected: int
) -> None:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.read().decode())
        return httpx.Response(
            200,
            json={"data": {"markdown": "the page", "metadata": {"title": "A page"}}},
        )

    reader = FirecrawlReader(
        client=client(httpx.MockTransport(record)),
        max_age_seconds=30 * 86_400,
    )

    fetched = dbutil.run(reader.fetch(cast("AnyHttpUrl", _URL), fresh))

    assert f'"maxAge": {expected}' in seen[0].replace('"maxAge":', '"maxAge": ')
    assert '"onlyMainContent": true' in seen[0].replace(
        '"onlyMainContent":', '"onlyMainContent": '
    )
    assert '"markdown"' in seen[0]
    assert fetched.markdown == "the page"
    assert fetched.title == "A page"
    assert fetched.persistable is True
    assert fetched.provider == "firecrawl-reader"


def test_a_scrape_without_readable_text_is_a_refusal() -> None:
    reader = FirecrawlReader(
        client=client(responder({"data": {"markdown": None}})),
        max_age_seconds=0,
    )

    with pytest.raises(ProviderUnavailable):
        dbutil.run(reader.fetch(cast("AnyHttpUrl", _URL), False))


def test_the_firecrawl_reader_needs_a_key_and_sizes_its_window_from_freshness() -> None:
    assert (
        FirecrawlReader.from_settings(
            configured(web_firecrawl_api_key=SecretStr("")), Freshness.stable
        )
        is None
    )
    built = FirecrawlReader.from_settings(configured(), Freshness.volatile)

    assert built is not None
    assert built.max_age_seconds == settings.web_search_volatile_days * 86_400


@pytest.mark.parametrize(
    ("freshness", "field"),
    [
        (Freshness.stable, "web_search_stable_days"),
        (Freshness.dated, "web_search_dated_days"),
        (Freshness.volatile, "web_search_volatile_days"),
    ],
)
def test_each_freshness_bucket_names_its_own_lifetime(freshness: Freshness, field: str) -> None:
    assert cache_days(settings, freshness) == getattr(settings, field)


class StubReader:
    """An artifact reader double answering with fixed bytes or one refusal."""

    def __init__(
        self, artifact: ArtifactBytes | None = None, error: Exception | None = None
    ) -> None:
        self.artifact = artifact
        self.error = error

    async def read_uri(self, source: URISource) -> ArtifactBytes:
        del source
        if self.error is not None:
            raise self.error
        assert self.artifact is not None
        return self.artifact


class StubConverter:
    """A Docling client double answering with one fixed conversion response."""

    def __init__(self, response: DoclingResponse) -> None:
        self.response = response

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        del artifact
        return self.response


def converted(markdown: str = "converted page") -> DoclingResponse:
    """One successful Docling response carrying both requested formats."""
    return DoclingResponse(
        document=DoclingDocument(md_content=markdown, json_content={"texts": []}),
        status="success",
    )


def docling_reader(reader: StubReader, response: DoclingResponse | None = None) -> DoclingReader:
    """A house reader over two doubles that satisfy the reader and converter surfaces."""
    return DoclingReader(reader=reader, converter=StubConverter(response or converted()))


def test_the_house_reader_converts_a_public_page_and_keeps_its_filename() -> None:
    reader = docling_reader(
        StubReader(ArtifactBytes(content=b"<html>", filename="page.html", media_type="text/html"))
    )

    fetched = dbutil.run(reader.fetch(cast("AnyHttpUrl", _URL), True))

    assert fetched.markdown == "converted page\n"
    assert fetched.title == "page.html"
    assert fetched.provider == "docling-reader"
    assert fetched.persistable is True


def test_the_house_reader_drops_the_placeholder_filename_as_a_title() -> None:
    reader = docling_reader(
        StubReader(ArtifactBytes(content=b"x", filename="artifact", media_type="text/html"))
    )

    assert dbutil.run(reader.fetch(cast("AnyHttpUrl", _URL), False)).title is None


@pytest.mark.parametrize(
    "error",
    [
        UnsafeArtifactError("private address"),
        httpx.ConnectError("no route"),
        DoclingConversionError("no text"),
        ValueError("unreadable"),
    ],
)
def test_every_house_reader_failure_is_a_skippable_refusal(error: Exception) -> None:
    reader = docling_reader(StubReader(error=error))

    with pytest.raises(ProviderUnavailable):
        dbutil.run(reader.fetch(cast("AnyHttpUrl", _URL), False))


def test_a_search_row_renders_its_own_preview_when_no_page_could_be_read() -> None:
    titled = WebResult(url=cast("AnyHttpUrl", _URL), title="A page", snippet="a snippet")
    bare = WebResult(url=cast("AnyHttpUrl", _URL))

    assert titled.preview == "A page\na snippet"
    assert bare.preview == _URL


def test_a_page_is_cut_to_its_rendering_bound_and_marked_when_it_was() -> None:
    fetched = WebPage(
        url=cast("AnyHttpUrl", _URL),
        markdown="x" * 20,
        provider="p",
        retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert fetched.trimmed(20) == "x" * 20
    assert fetched.trimmed(5) == "xxxxx…"


def test_interned_provider_clients_close_once_and_rebuild_afterwards() -> None:
    first = web_client("https://provider.test", 5.0, (("Authorization", "Bearer k"),))

    assert web_client("https://provider.test", 5.0, (("Authorization", "Bearer k"),)) is first

    dbutil.run(close_web_clients())

    assert first.is_closed
    assert web_client("https://provider.test", 5.0, ()) is not first
    dbutil.run(close_web_clients())
