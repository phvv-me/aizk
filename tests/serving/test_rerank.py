import json
from importlib import import_module

import httpx
import pytest
from dbutil import run

from aizk.config import Settings, settings
from aizk.serving.rerank import rerank

reranker_module = import_module("aizk.serving.rerank.reranker")


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
        transport=httpx.MockTransport(respond), base_url="http://rerank.test/v1"
    )
    monkeypatch.setattr(reranker_module, "client", lambda: client)
    return requests


def test_rerank_scores_come_back_aligned_to_the_input_order(rerank_endpoint: list[dict]) -> None:
    scores = run(rerank("what holds", ["first", "second", "third"]))

    assert scores == [1.0, 0.5, 1.0 / 3]
    [request] = rerank_endpoint
    assert request["model"] == settings.rerank_model
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
        {"model": settings.rerank_model, "query": "what holds", "documents": ["first"]}
    ]


def test_rerank_short_circuits_on_no_texts(rerank_endpoint: list[dict]) -> None:
    assert run(rerank("anything", [])) == []
    assert rerank_endpoint == []


def test_rerank_rejects_a_score_count_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1.0}]})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://rerank.test/v1"
    )
    monkeypatch.setattr(reranker_module, "client", lambda: client)

    with pytest.raises(ValueError, match="1 scores for 2 texts"):
        run(rerank("query", ["one", "two"]))


def test_client_carries_the_configured_key_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reranker_module.client.cache_clear()
    monkeypatch.setattr(settings, "rerank_url", "http://rerank.test/v1")
    monkeypatch.setattr(settings, "rerank_api_key", "secret")

    client = reranker_module.client()

    assert str(client.base_url) == "http://rerank.test/v1/"
    assert client.headers["authorization"] == "Bearer secret"
    reranker_module.client.cache_clear()


def test_settings_defaults_keep_rerank_off_without_an_endpoint() -> None:
    assert Settings(_env_file=None).rerank_url == ""
