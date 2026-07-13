import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from itertools import batched
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image
from strategies import short_text

from aizk.config import settings
from aizk.serving.chunk import chunk_code, chunk_text, is_code, is_text
from aizk.serving.chunk.chunker import file_tags
from aizk.serving.embed import embed, embed_images, image_url_for, instructed

chonkie_module = import_module("aizk.serving.chunk.chonkie")
code_module = import_module("aizk.serving.chunk.code")
embedder_module = import_module("aizk.serving.embed.embedder")


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
        self.posts: list[tuple[str, dict[str, object]]] = []

    async def post(self, path: str, *, cast_to: type, body: dict[str, object]) -> SimpleNamespace:
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
    assert instructed(texts, "") == list(texts)
    wrapped = instructed(texts, instruction)
    assert wrapped == [f"Instruct: {instruction}\nQuery: {text}" for text in texts]


@given(embed_dim=st.integers(min_value=1, max_value=6), texts=st.lists(short_text, min_size=1))
def test_embed_batches_realigns_and_carries_the_width(
    monkeypatch: pytest.MonkeyPatch, embed_dim: int, texts: list[str]
) -> None:
    monkeypatch.setattr(settings, "embed_model", "test-embed")
    monkeypatch.setattr(settings, "embed_dim", embed_dim)
    monkeypatch.setattr(settings, "embed_batch_size", 3)
    monkeypatch.setattr(settings, "embed_instruction_document", "")
    client = FakeEmbedClient()
    monkeypatch.setattr(embedder_module, "_client", lambda: client)

    vectors = asyncio.run(embed(texts))

    assert len(vectors) == len(texts)
    assert all(len(vector) == embed_dim for vector in vectors)
    lead = [float(i) for batch in batched(texts, 3, strict=False) for i in range(len(batch))]
    assert [vector[0] for vector in vectors] == lead
    calls = client.embeddings.calls
    assert [text for call in calls for text in call.input] == list(texts)
    for call in calls:
        assert call.dimensions == embed_dim
        assert call.encoding_format == "float"
        assert call.model == "test-embed"
        assert len(call.input) <= 3


def test_embed_query_mode_wraps_the_instruct_query_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "embed_model", "test-embed")
    monkeypatch.setattr(settings, "embed_dim", 2)
    monkeypatch.setattr(settings, "embed_instruction_query", "retrieve relevant passages")
    client = FakeEmbedClient()
    monkeypatch.setattr(embedder_module, "_client", lambda: client)

    asyncio.run(embed(["find the fact"], mode="query"))

    assert client.embeddings.calls[0].input == [
        "Instruct: retrieve relevant passages\nQuery: find the fact"
    ]


@pytest.mark.parametrize(
    "value",
    ["http://pics.test/cat.png", "https://pics.test/cat.png", "data:image/png;base64,AAAA"],
)
def test_image_url_for_passes_urls_and_data_uris_through(value: str) -> None:
    assert image_url_for(value) == value


def test_image_url_for_encodes_paths_and_pil_images(tmp_path: Path) -> None:
    jpg = tmp_path / "pixel.jpg"
    Image.new("RGB", (1, 1)).save(jpg)
    unknown = tmp_path / "pixel.weirdext"
    unknown.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert image_url_for(str(jpg)).startswith("data:image/jpeg;base64,")
    assert image_url_for(str(unknown)).startswith("data:image/png;base64,")
    assert image_url_for(Image.new("RGB", (1, 1))).startswith("data:image/png;base64,")


def test_embed_images_posts_a_chat_body_and_parses_the_pooled_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "embed_model", "img-embed")
    monkeypatch.setattr(settings, "embed_dim", 2)
    monkeypatch.setattr(settings, "embed_instruction_document", "represent the document")
    client = FakeEmbedClient()
    monkeypatch.setattr(embedder_module, "_client", lambda: client)

    [vector] = asyncio.run(embed_images(["https://pics.test/cat.png"]))

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
    embedder_module._client.cache_clear()
    client = embedder_module._client()

    assert embedder_module._client() is client
    assert str(client.base_url) == "http://carry.test/v1/"
    assert client.api_key == "secret"
    embedder_module._client.cache_clear()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("module.py", True),
        ("module.rs", True),
        ("module.go", True),
        ("module.ts", True),
        ("module.java", True),
        ("module.c", True),
        ("module.sh", True),
        ("module.sql", True),
        ("MODULE.PY", True),
        ("note.md", False),
        ("data.json", False),
        ("conf.yaml", False),
        ("conf.toml", False),
        ("page.html", False),
        ("table.csv", False),
        ("mystery.definitely-not-a-real-suffix", False),
    ],
)
def test_is_code_admits_source_and_rejects_markup_data_and_unknown(
    name: str, expected: bool
) -> None:
    assert is_code(Path(name)) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("module.py", True),
        ("note.md", True),
        ("data.json", True),
        ("scan.pdf", False),
        ("archive.bin", False),
        ("mystery.definitely-not-a-real-suffix", False),
    ],
)
def test_is_text_admits_code_and_markup_and_rejects_binary(name: str, expected: bool) -> None:
    assert is_text(Path(name)) is expected


def test_file_tags_sniffs_content_of_a_real_file(tmp_path: Path) -> None:
    script = tmp_path / "script"
    script.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    script.chmod(0o755)

    assert file_tags(Path(script.name)) == file_tags(tmp_path / "missing")
    assert "python" in file_tags(script)
    assert is_text(script)
    assert is_code(script)


def test_chunk_functions_strip_and_drop_empty_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = StubInner(["  a  ", "   ", "", "b\n", "\tc"])
    monkeypatch.setattr(chonkie_module, "_chunker", lambda: inner)
    monkeypatch.setattr(code_module, "_chunker", lambda: inner)

    assert chunk_text("ignored") == ["a", "b", "c"]
    assert chunk_code("ignored") == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("chunk", "text", "needle"),
    [
        (
            chunk_text,
            "The Leech lattice packs spheres.\n\nThe Conway group is sporadic.",
            "Leech",
        ),
        (chunk_code, "def a(x):\n    return x + 1\n\n\ndef b(y):\n    return y * 2\n", "def a"),
    ],
)
def test_chunk_real_backend_returns_clean_spans(
    chunk: Callable[[str], list[str]], text: str, needle: str
) -> None:
    spans = chunk(text)

    assert spans
    assert all(span == span.strip() and span for span in spans)
    assert any(needle in span for span in spans)
