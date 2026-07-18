import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from itertools import batched
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image
from pydantic import JsonValue
from strategies import short_text

from aizk.config import settings
from aizk.serving.base import openai_client
from aizk.serving.chunk import chunk_text, is_code, is_text
from aizk.serving.chunk.chunker import file_tags
from aizk.serving.embed import EmbedClient, EmbedMode, ImageBytes

base_module = import_module("aizk.serving.base")
chonkie_module = import_module("aizk.serving.chunk.chonkie")
embed_module = import_module("aizk.serving.embed.client")


@dataclass
class EmbedCall:
    model: str
    input: list[str]
    dimensions: int
    encoding_format: str


class RecordingEmbeddings:
    def __init__(self) -> None:
        self.calls: list[EmbedCall] = []

    async def create(
        self, *, model: str, input: list[str], dimensions: int, encoding_format: str
    ) -> SimpleNamespace:
        self.calls.append(EmbedCall(model, input, dimensions, encoding_format))
        rows = [
            SimpleNamespace(index=i, embedding=[float(i), *([0.0] * (dimensions - 1))])
            for i in range(len(input))
        ]
        return SimpleNamespace(data=list(reversed(rows)))


class FakeEmbedClient:
    def __init__(self) -> None:
        self.embeddings = RecordingEmbeddings()
        self.posts: list[tuple[str, dict[str, JsonValue]]] = []

    async def post(
        self, path: str, *, cast_to: type, body: dict[str, JsonValue]
    ) -> SimpleNamespace:
        self.posts.append((path, body))
        dim = cast("int", body["dimensions"])
        return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.5] * dim)])


class StubInner:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts

    def chunk(self, text: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(text=raw) for raw in self.texts]


@given(texts=st.lists(short_text, max_size=6), instruction=short_text)
def test_instructed_wraps_each_text_and_empty_is_identity(
    texts: list[str], instruction: str
) -> None:
    assert EmbedClient.instructed(texts, "") == list(texts)
    wrapped = EmbedClient.instructed(texts, instruction)
    assert wrapped == [f"Instruct: {instruction}\nQuery: {text}" for text in texts]


@pytest.mark.parametrize(
    ("mode", "instruction"),
    [("document", ""), ("query", "retrieve relevant passages")],
)
@given(embed_dim=st.integers(min_value=1, max_value=6), texts=st.lists(short_text, min_size=1))
def test_embed_batches_realigns_and_carries_the_width(
    monkeypatch: pytest.MonkeyPatch,
    embed_dim: int,
    texts: list[str],
    mode: EmbedMode,
    instruction: str,
) -> None:
    monkeypatch.setattr(settings, "embed_model", "test-embed")
    monkeypatch.setattr(settings, "embed_dim", embed_dim)
    monkeypatch.setattr(settings, "embed_batch_size", 3)
    monkeypatch.setattr(settings, f"embed_instruction_{mode}", instruction)
    client = FakeEmbedClient()
    monkeypatch.setattr(embed_module, "openai_client", lambda *args: client)

    vectors = asyncio.run(EmbedClient.from_settings(settings).embed(texts, mode=mode))

    assert len(vectors) == len(texts)
    assert all(len(vector) == embed_dim for vector in vectors)
    lead = [float(i) for batch in batched(texts, 3, strict=False) for i in range(len(batch))]
    assert [vector[0] for vector in vectors] == lead
    calls = client.embeddings.calls
    assert [text for call in calls for text in call.input] == EmbedClient.instructed(
        texts, instruction
    )
    for call in calls:
        assert call.dimensions == embed_dim
        assert call.encoding_format == "float"
        assert call.model == "test-embed"
        assert len(call.input) <= 3


def test_image_url_normalizes_every_supported_source_type(tmp_path: Path) -> None:
    passthrough = (
        "http://pics.test/cat.png",
        "https://pics.test/cat.png",
        "data:image/png;base64,AAAA",
    )
    assert [EmbedClient.image_url(value) for value in passthrough] == list(passthrough)

    jpg = tmp_path / "pixel.jpg"
    Image.new("RGB", (1, 1)).save(jpg)
    unknown = tmp_path / "pixel.weirdext"
    unknown.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert EmbedClient.image_url(str(jpg)).startswith("data:image/jpeg;base64,")
    assert EmbedClient.image_url(str(unknown)).startswith("data:image/png;base64,")
    assert EmbedClient.image_url(Image.new("RGB", (1, 1))).startswith("data:image/png;base64,")
    assert (
        EmbedClient.image_url(ImageBytes(content=b"\x89PNG", media_type="image/png"))
        == "data:image/png;base64,iVBORw=="
    )

    with pytest.raises(ValueError, match="image media type"):
        ImageBytes(content=b"video", media_type="video/mp4")


def test_embed_images_posts_a_chat_body_and_parses_the_pooled_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "embed_model", "img-embed")
    monkeypatch.setattr(settings, "embed_dim", 2)
    monkeypatch.setattr(settings, "embed_instruction_document", "represent the document")
    client = FakeEmbedClient()
    monkeypatch.setattr(embed_module, "openai_client", lambda *args: client)

    [vector] = asyncio.run(
        EmbedClient.from_settings(settings).embed_images(["https://pics.test/cat.png"])
    )

    assert vector == [0.5, 0.5]
    path, body = client.posts[0]
    assert path == "/embeddings"
    assert body == {
        "model": "img-embed",
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "represent the document"}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://pics.test/cat.png"},
                    },
                    {"type": "text", "text": ""},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        ],
        "dimensions": 2,
        "encoding_format": "float",
        "continue_final_message": True,
        "add_special_tokens": True,
    }


def test_embedding_client_is_cached_and_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "embed_url", "http://carry.test/v1")
    monkeypatch.setattr(settings, "embed_api_key", "secret")
    openai_client.cache_clear()
    client = openai_client(
        settings.embed_url, settings.embed_api_key, settings.embed_request_timeout
    )

    assert (
        openai_client(settings.embed_url, settings.embed_api_key, settings.embed_request_timeout)
        is client
    )
    assert str(client.base_url) == "http://carry.test/v1/"
    assert client.api_key == "secret"
    openai_client.cache_clear()


@pytest.mark.parametrize(
    ("predicate", "name", "expected"),
    [
        (is_code, "module.py", True),
        (is_code, "module.rs", True),
        (is_code, "module.go", True),
        (is_code, "module.ts", True),
        (is_code, "module.java", True),
        (is_code, "module.c", True),
        (is_code, "module.sh", True),
        (is_code, "module.sql", True),
        (is_code, "MODULE.PY", True),
        (is_code, "note.md", False),
        (is_code, "data.json", False),
        (is_code, "conf.yaml", False),
        (is_code, "conf.toml", False),
        (is_code, "page.html", False),
        (is_code, "table.csv", False),
        (is_code, "mystery.definitely-not-a-real-suffix", False),
        (is_text, "module.py", True),
        (is_text, "note.md", True),
        (is_text, "data.json", True),
        (is_text, "scan.pdf", False),
        (is_text, "archive.bin", False),
        (is_text, "mystery.definitely-not-a-real-suffix", False),
    ],
)
def test_file_classifiers_cover_source_text_and_binary_boundaries(
    predicate: Callable[[Path], bool], name: str, expected: bool
) -> None:
    assert predicate(Path(name)) is expected


def test_file_tags_sniffs_content_of_a_real_file(tmp_path: Path) -> None:
    script = tmp_path / "script"
    script.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    script.chmod(0o755)

    assert file_tags(Path(script.name)) == file_tags(tmp_path / "missing")
    assert "python" in file_tags(script)
    assert is_text(script)
    assert is_code(script)


def test_chunk_function_strips_and_drops_empty_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = StubInner(["  a  ", "   ", "", "b\n", "\tc"])
    monkeypatch.setattr(chonkie_module, "_chunker", lambda chunk_size: inner)

    assert chunk_text("ignored", 512) == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("chunk", "text", "needle"),
    [
        (
            chunk_text,
            "The Leech lattice packs spheres.\n\nThe Conway group is sporadic.",
            "Leech",
        ),
        (chunk_text, "def a(x):\n    return x + 1\n\n\ndef b(y):\n    return y * 2\n", "def a"),
    ],
)
def test_chunk_real_backend_returns_clean_spans(
    chunk: Callable[[str], list[str]], text: str, needle: str
) -> None:
    spans = chunk(text)

    assert spans
    assert all(span == span.strip() and span for span in spans)
    assert any(needle in span for span in spans)


def test_close_clients_closes_each_once_and_resets_the_interning() -> None:
    class Exploding(httpx.AsyncClient):
        async def aclose(self) -> None:
            raise RuntimeError("Event loop is closed")

    async def body() -> None:
        interned = base_module.openai_client("http://close.test/v1", "", 1.0)
        assert base_module.openai_client("http://close.test/v1", "", 1.0) is interned
        sidecar = base_module.http_client("http://close.test", "", 1.0)
        base_module._open_clients.append(Exploding())
        await base_module.close_clients()
        assert base_module._open_clients == []
        assert sidecar.is_closed
        assert base_module.openai_client("http://close.test/v1", "", 1.0) is not interned
        await base_module.close_clients()

    asyncio.run(body())
