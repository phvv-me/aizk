import asyncio
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import batched
from pathlib import Path

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image
from strategies import short_text

from aizk.config import settings as global_settings
from aizk.serving.embed import Embedder, image_url_for, instructed, instruction_for

EMBED_URL = "http://embed.test/v1"
EMBEDDINGS = f"{EMBED_URL}/embeddings"

# a one-pixel PNG written once to a temp path, the local image file the image lane base64-inlines
WRITTEN_IMAGE = Path(tempfile.gettempdir()) / "aizk_test_pixel.png"
Image.new("RGB", (1, 1)).save(WRITTEN_IMAGE)


@contextmanager
def fresh_embedder(**fields: bool | int | float | str) -> Iterator[Embedder]:
    """Build an `Embedder` under overridden settings, clearing any prior cached instance both ways.

    `Embedder` is a `patos` singleton, one shared instance per class forever after the first
    construction, so a test that needs a client built from a specific url, model, dim, or key
    clears the cached slot before constructing and again on exit, leaving no test-configured
    client behind for a later test's real `Embedder()` to reuse. A manual `pytest.MonkeyPatch`
    stands in for the deleted `override`, since this helper is not itself a fixture.

    fields: `Settings` fields temporarily set for the duration of the block.
    """
    patch = pytest.MonkeyPatch()
    for key, value in fields.items():
        patch.setattr(global_settings, key, value)
    if "singleton_instance" in Embedder.__dict__:
        delattr(Embedder, "singleton_instance")
    try:
        yield Embedder()
    finally:
        if "singleton_instance" in Embedder.__dict__:
            delattr(Embedder, "singleton_instance")
        patch.undo()


def openai_reply(rows: list[tuple[int, list[float]]]) -> dict[str, object]:
    """Shape an OpenAI-style embeddings response body from index and vector pairs.

    rows: the (index, embedding) pairs to wrap as response data, in any order.
    """
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector} for index, vector in rows
        ],
        "model": "test-embed",
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


@given(
    embed_dim=st.integers(min_value=1, max_value=6),
    texts=st.lists(short_text, min_size=1, max_size=70),
)
def test_embedder_sends_embed_dim_and_realigns_rows_per_batch(
    embed_dim: int, texts: list[str]
) -> None:
    """Every request carries dimensions==embed_dim and the rows realign to the input order.

    The keystone of the dimensions invariant and the row alignment contract at once. The fake
    server returns each batch's rows reversed by index, so a result back in 0..n order per batch
    proves the embedder re-sorts by the returned index.
    """

    def echo(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        rows = [
            (index, [float(index), *([0.0] * (embed_dim - 1))]) for index in range(len(inputs))
        ]
        return httpx.Response(200, json=openai_reply(list(reversed(rows))))

    with respx.mock:
        route = respx.post(EMBEDDINGS).mock(side_effect=echo)
        with fresh_embedder(embed_url=EMBED_URL, embed_model="test-embed", embed_dim=embed_dim):
            vectors = asyncio.run(Embedder().embed(texts))

    assert len(vectors) == len(texts)
    assert all(len(vector) == embed_dim for vector in vectors)
    batch_size = global_settings.embed_batch_size
    lead = [
        float(i) for batch in batched(texts, batch_size, strict=False) for i in range(len(batch))
    ]
    assert [vector[0] for vector in vectors] == lead
    sent_texts = set(texts)
    for call in route.calls:
        sent = json.loads(call.request.content)
        assert sent["dimensions"] == embed_dim
        assert sent["encoding_format"] == "float"
        assert sent["model"] == "test-embed"
        # document mode's default empty instruction leaves every text plain, the Qwen3-Embedding
        # recipe's document lane, so the exact input text rides through untouched
        assert all(item in sent_texts for item in sent["input"])


@respx.mock
@pytest.mark.parametrize("api_key", ["secret", ""])
def test_embedder_sends_a_bearer_header_only_with_a_key(api_key: str) -> None:
    """A configured key rides as Authorization Bearer, an empty key leaves the header off."""
    route = respx.post(EMBEDDINGS).respond(json=openai_reply([(0, [1.0, 0.0])]))

    with fresh_embedder(
        embed_url=EMBED_URL, embed_model="test-embed", embed_dim=2, embed_api_key=api_key
    ) as embedder:
        asyncio.run(embedder.embed(["a"]))

    header = route.calls.last.request.headers
    assert header.get("authorization") == (f"Bearer {api_key}" if api_key else None)


@respx.mock
def test_embedder_image_lane_posts_chat_messages_with_the_width_and_instruction() -> None:
    """The image lane POSTs a chat messages body carrying the image_url, width, and instruction."""
    route = respx.post(EMBEDDINGS).respond(json=openai_reply([(0, [0.5, 0.5])]))

    with fresh_embedder(embed_url=EMBED_URL, embed_model="test-embed", embed_dim=2) as embedder:
        [vector] = asyncio.run(embedder.embed_images(["https://pics.test/cat.png"]))

    assert vector == [0.5, 0.5]
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "test-embed"
    assert sent["dimensions"] == 2
    assert sent["encoding_format"] == "float"
    user_turn = next(turn for turn in sent["messages"] if turn["role"] == "user")
    image_part = next(part for part in user_turn["content"] if part["type"] == "image_url")
    assert image_part["image_url"]["url"] == "https://pics.test/cat.png"
    system_turn = next(turn for turn in sent["messages"] if turn["role"] == "system")
    assert system_turn["content"][0]["text"] == global_settings.embed_instruction_document


def test_instruction_for_picks_query_or_document_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`instruction_for` reads the mode-matched settings field, query or document."""
    monkeypatch.setattr(global_settings, "embed_instruction_document", "doc instruction")
    monkeypatch.setattr(global_settings, "embed_instruction_query", "query instruction")
    assert instruction_for("document") == "doc instruction"
    assert instruction_for("query") == "query instruction"


def test_instructed_leaves_text_plain_with_no_instruction() -> None:
    """An empty instruction, embed_instruction_document's default, wraps nothing."""
    assert instructed(["a chunk", "another chunk"], "") == ["a chunk", "another chunk"]


def test_instructed_wraps_every_text_in_the_instruct_query_prefix() -> None:
    """A non-empty instruction wraps every text in the Qwen3-Embedding Instruct/Query prefix."""
    wrapped = instructed(["a chunk", "another chunk"], "retrieve relevant passages")
    assert wrapped == [
        "Instruct: retrieve relevant passages\nQuery: a chunk",
        "Instruct: retrieve relevant passages\nQuery: another chunk",
    ]


@respx.mock
def test_embedder_query_mode_sends_the_instruct_query_prefix() -> None:
    """A query embed wraps every text in the query instruction's Instruct/Query prefix."""
    route = respx.post(EMBEDDINGS).respond(json=openai_reply([(0, [1.0, 0.0])]))

    with fresh_embedder(
        embed_url=EMBED_URL,
        embed_model="test-embed",
        embed_dim=2,
        embed_instruction_query="retrieve relevant passages",
    ):
        asyncio.run(Embedder().embed(["find the fact"], mode="query"))

    sent = json.loads(route.calls.last.request.content)
    assert sent["input"] == ["Instruct: retrieve relevant passages\nQuery: find the fact"]


@respx.mock
def test_embedder_document_mode_stays_plain_by_default() -> None:
    """A document embed under the default empty embed_instruction_document sends the raw text."""
    route = respx.post(EMBEDDINGS).respond(json=openai_reply([(0, [1.0, 0.0])]))

    with fresh_embedder(
        embed_url=EMBED_URL, embed_model="test-embed", embed_dim=2, embed_instruction_document=""
    ):
        asyncio.run(Embedder().embed(["a stored chunk"], mode="document"))

    sent = json.loads(route.calls.last.request.content)
    assert sent["input"] == ["a stored chunk"]


@respx.mock
def test_embedder_document_mode_wraps_when_an_instruction_is_configured() -> None:
    """Setting embed_instruction_document opts documents into the same Instruct/Query shape."""
    route = respx.post(EMBEDDINGS).respond(json=openai_reply([(0, [1.0, 0.0])]))

    with fresh_embedder(
        embed_url=EMBED_URL,
        embed_model="test-embed",
        embed_dim=2,
        embed_instruction_document="represent the document",
    ):
        asyncio.run(Embedder().embed(["a stored chunk"], mode="document"))

    sent = json.loads(route.calls.last.request.content)
    assert sent["input"] == ["Instruct: represent the document\nQuery: a stored chunk"]


@pytest.mark.parametrize(
    "value",
    ["https://pics.test/cat.png", "http://pics.test/cat.png", "data:image/png;base64,AAAA"],
)
def test_image_url_for_passes_a_url_or_data_uri_through_untouched(value: str) -> None:
    """A url or an already-formed data URI rides untouched, never re-read or re-encoded."""
    assert image_url_for(value) == value


@pytest.mark.parametrize("image", [str(WRITTEN_IMAGE), Image.new("RGB", (1, 1))])
def test_image_url_for_inlines_local_bytes_as_a_png_data_uri(image: str | Image.Image) -> None:
    """A filesystem path and a PIL image are base64-inlined so the server needs no path access."""
    assert image_url_for(image).startswith("data:image/png;base64,")


def test_embedder_carries_the_url_model_dim_and_key_from_settings() -> None:
    """`Embedder()` threads the live embed url, model, width, and key from settings."""
    with fresh_embedder(
        embed_url="http://carry.test/v1",
        embed_model="carry-embed",
        embed_dim=2,
        embed_api_key="secret",
    ) as embedder:
        assert (
            embedder.embed_url,
            embedder.embed_model,
            embedder.embed_dim,
            embedder.api_key,
        ) == ("http://carry.test/v1", "carry-embed", 2, "secret")


def test_embedder_is_a_singleton_per_class() -> None:
    """Two `Embedder()` calls under the same settings return the exact same shared instance."""
    with fresh_embedder(embed_url="http://cache.test/v1", embed_model="cache-embed", embed_dim=2):
        assert Embedder() is Embedder()
