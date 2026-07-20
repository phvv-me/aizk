import json
from importlib import import_module

import httpx
import pytest
from dbutil import run

from aizk.config import Settings, settings
from aizk.serving.base import http_client
from aizk.serving.rerank import RerankClient

rerank_module = import_module("aizk.serving.rerank.client")

# Builds the real rerank client over an in-test transport double, so it opts out of the default
# model-lane stubbing.
pytestmark = pytest.mark.real_services


async def rerank(query: str, texts: list[str]) -> list[float]:
    return await RerankClient.from_settings(settings).rerank(query, texts)


@pytest.fixture
def rerank_endpoint(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Route the rerank client to an in-memory endpoint scoring by document order."""
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        results = [
            {"index": index, "relevance_score": 1.0 / (1 + index)}
            for index in range(len(payload["documents"]))
        ]
        return httpx.Response(200, json={"results": list(reversed(results))})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://rerank.test"
    )
    monkeypatch.setattr(rerank_module, "http_client", lambda *args: client)
    return requests


def test_rerank_scores_come_back_aligned_to_the_input_order(rerank_endpoint: list[dict]) -> None:
    scores = run(rerank("what holds", ["first", "second", "third"]))

    assert scores == [1.0, 0.5, 1.0 / 3]
    [request] = rerank_endpoint
    assert request["model"] == settings.rerank_model
    assert request["max_tokens_per_query"] == settings.rerank_query_max_tokens
    assert request["max_tokens_per_doc"] == settings.rerank_document_max_tokens
    assert request["truncate_prompt_tokens"] == -1
    assert request["truncation_side"] == "left"
    assert request["query"] == settings.rerank_query_template.format(
        instruction=settings.rerank_instruction, query="what holds"
    )
    assert request["documents"] == [
        settings.rerank_document_template.format(document=text)
        for text in ("first", "second", "third")
    ]


def test_rerank_sends_raw_texts_when_the_templates_are_empty(
    rerank_endpoint: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rerank_query_template", "")
    monkeypatch.setattr(settings, "rerank_document_template", "")

    run(rerank("what holds", ["first"]))

    assert rerank_endpoint == [
        {
            "model": settings.rerank_model,
            "query": "what holds",
            "documents": ["first"],
            "max_tokens_per_query": settings.rerank_query_max_tokens,
            "max_tokens_per_doc": settings.rerank_document_max_tokens,
            "truncate_prompt_tokens": -1,
            "truncation_side": "left",
        }
    ]


def test_rerank_short_circuits_on_no_texts(rerank_endpoint: list[dict]) -> None:
    assert run(rerank("anything", [])) == []
    assert rerank_endpoint == []


@pytest.mark.parametrize(
    ("results", "message"),
    [
        ([{"index": 0, "relevance_score": 1.0}], "1 scores for 2 texts"),
        (
            [
                {"index": 0, "relevance_score": 1.0},
                {"index": 0, "relevance_score": 0.5},
            ],
            "invalid result indexes",
        ),
        (
            [
                {"index": 0, "relevance_score": 1.0},
                {"index": 2, "relevance_score": 0.5},
            ],
            "invalid result indexes",
        ),
    ],
    ids=["missing", "duplicate", "out-of-range"],
)
def test_rerank_rejects_invalid_result_sets(
    results: list[dict], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": results})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://rerank.test"
    )
    monkeypatch.setattr(rerank_module, "http_client", lambda *args: client)

    with pytest.raises(ValueError, match=message):
        run(rerank("query", ["one", "two"]))


def test_client_carries_the_configured_key_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_client.cache_clear()
    monkeypatch.setattr(settings, "rerank_url", "http://rerank.test")
    monkeypatch.setattr(settings, "rerank_api_key", "secret")

    client = http_client(
        settings.rerank_url,
        settings.rerank_api_key,
        settings.rerank_request_timeout,
    )

    assert str(client.base_url) == "http://rerank.test/"
    assert client.headers["authorization"] == "Bearer secret"
    assert Settings(_env_file=None).rerank_url == ""
    http_client.cache_clear()
