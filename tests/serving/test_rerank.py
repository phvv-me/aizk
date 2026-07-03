import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st
from strategies import short_text

from aizk.config import settings as global_settings
from aizk.serving.rerank import Reranker

RERANK_URL = "http://rerank.test/v1"
RERANK_ENDPOINT = f"{RERANK_URL}/rerank"


@contextmanager
def fresh_reranker(**fields: bool | int | float | str) -> Iterator[Reranker]:
    """Build a `Reranker` under overridden settings, clearing any cached instance before and after.

    `Reranker` is a `patos` singleton, one shared instance per class forever after the first
    construction, so a test that needs a client built from a specific url, model, or key clears
    the cached slot before constructing and again on exit, leaving no test-configured client
    behind for a later test's real `Reranker()` to reuse. A manual `pytest.MonkeyPatch` stands in
    for the deleted `override`, since this helper is not itself a fixture.

    fields: `Settings` fields temporarily set for the duration of the block.
    """
    patch = pytest.MonkeyPatch()
    for key, value in fields.items():
        patch.setattr(global_settings, key, value)
    if "singleton_instance" in Reranker.__dict__:
        delattr(Reranker, "singleton_instance")
    try:
        yield Reranker()
    finally:
        if "singleton_instance" in Reranker.__dict__:
            delattr(Reranker, "singleton_instance")
        patch.undo()


@given(query=short_text, candidates=st.lists(short_text, min_size=1, max_size=8))
def test_reranker_posts_the_cohere_shape_and_realigns_scores_by_index(
    query: str, candidates: list[str]
) -> None:
    """The reranker POSTs the served model, query, and documents, then realigns by index.

    The fake server scores each document by its position and returns the results reversed, so a
    score list back in input order proves the reranker re-sorts by the returned index rather than
    trusting the array order.
    """

    def echo(request: httpx.Request) -> httpx.Response:
        documents = json.loads(request.content)["documents"]
        results = [{"index": i, "relevance_score": float(i)} for i in range(len(documents))]
        return httpx.Response(200, json={"results": list(reversed(results))})

    with respx.mock:
        route = respx.post(RERANK_ENDPOINT).mock(side_effect=echo)
        with fresh_reranker(rerank_url=RERANK_URL, rerank_model="bge-reranker") as reranker:
            scores = asyncio.run(reranker.rerank(query, candidates))

    assert scores == [float(i) for i in range(len(candidates))]
    assert json.loads(route.calls.last.request.content) == {
        "model": "bge-reranker",
        "query": query,
        "documents": candidates,
    }


@respx.mock
@pytest.mark.parametrize("api_key", ["secret", ""])
def test_reranker_sends_a_bearer_header_only_with_a_key(api_key: str) -> None:
    """A configured key rides as Authorization Bearer, an empty key leaves the header off."""
    route = respx.post(RERANK_ENDPOINT).respond(
        json={"results": [{"index": 0, "relevance_score": 0.5}]}
    )

    with fresh_reranker(
        rerank_url=RERANK_URL, rerank_model="bge", rerank_api_key=api_key
    ) as reranker:
        asyncio.run(reranker.rerank("q", ["d"]))

    header = route.calls.last.request.headers
    assert header.get("authorization") == (f"Bearer {api_key}" if api_key else None)


def test_reranker_skips_the_call_with_no_candidates() -> None:
    """An empty candidate list returns no scores and makes no HTTP call, the short-circuit."""
    with fresh_reranker(rerank_url=RERANK_URL, rerank_model="bge") as reranker:
        assert asyncio.run(reranker.rerank("q", [])) == []


def test_reranker_carries_the_url_model_and_key_from_settings() -> None:
    """`Reranker()` threads the live rerank url, model, and key from settings into the client."""
    with fresh_reranker(
        rerank_url=RERANK_URL, rerank_model="bge-reranker", rerank_api_key="secret"
    ) as reranker:
        assert (reranker.rerank_url, reranker.rerank_model, reranker.api_key) == (
            RERANK_URL,
            "bge-reranker",
            "secret",
        )


def test_reranker_is_a_singleton_per_class() -> None:
    """Two `Reranker()` calls under the same settings return the exact same shared instance."""
    with fresh_reranker(rerank_url=RERANK_URL, rerank_model="cache-reranker"):
        assert Reranker() is Reranker()
