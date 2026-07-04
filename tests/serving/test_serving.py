import asyncio
from dataclasses import dataclass
from itertools import batched
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from openai import AsyncOpenAI
from PIL import Image
from strategies import short_text

from aizk.config import settings
from aizk.serving.chunk import ChonkieChunker, CodeChunker, is_code, is_text
from aizk.serving.chunk.chunker import file_tags
from aizk.serving.embed import Embedder, image_url_for, instructed
from aizk.serving.rerank import Reranker, RerankResponse, RerankResult


@dataclass
class EmbedCall:
    """One recorded `embeddings.create` call, the request body the text lane builds per batch.

    model: served model id sent.
    input: the (possibly instruction-wrapped) texts of this batch.
    dimensions: the width the request asked the server to truncate to.
    encoding_format: the pinned wire format.
    """

    model: str
    input: list[str]
    dimensions: int
    encoding_format: str


class RecordingEmbeddings:
    """The `client.embeddings` namespace `Embedder.embed` drives, recording every create call.

    Returns one row per input with its index encoded into `embedding[0]`, then hands the rows back
    reversed by index so a result in input order proves `embed` re-sorts by the returned index
    rather than trusting the array order.

    calls: the recorded create bodies, one per batch, in send order.
    """

    def __init__(self) -> None:
        self.calls: list[EmbedCall] = []

    async def create(
        self, *, model: str, input: list[str], dimensions: int, encoding_format: str
    ) -> SimpleNamespace:
        """Record the call and return `dimensions`-wide rows reversed by index."""
        self.calls.append(EmbedCall(model, input, dimensions, encoding_format))
        rows = [
            SimpleNamespace(index=i, embedding=[float(i), *([0.0] * (dimensions - 1))])
            for i in range(len(input))
        ]
        return SimpleNamespace(data=list(reversed(rows)))


class FakeEmbedClient:
    """An `AsyncOpenAI` stand-in exposing only the text `embeddings.create` and image `post` seams.

    embeddings: the recording text lane.
    posts: every image-lane post, as (path, body) pairs.
    """

    def __init__(self) -> None:
        self.embeddings = RecordingEmbeddings()
        self.posts: list[tuple[str, dict[str, object]]] = []

    async def post(self, path: str, *, cast_to: type, body: dict[str, object]) -> SimpleNamespace:
        """Record the image-lane post and return one `dimensions`-wide pooled row."""
        self.posts.append((path, body))
        dim = cast("int", body["dimensions"])
        return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.5] * dim)])


class FakeRerankClient:
    """An `AsyncOpenAI` stand-in exposing only the `post` seam `Reranker.rerank` drives.

    Scores each candidate `index + 0.5` and emits the results in `order`, an arbitrary
    permutation, so a score list back in input order proves `rerank` realigns by the returned
    index rather than the array order.

    order: the permutation of candidate positions the results come back in.
    posts: every post, as (path, body) pairs.
    """

    def __init__(self, order: list[int] | None = None) -> None:
        self.order = order
        self.posts: list[tuple[str, dict[str, object]]] = []

    async def post(self, path: str, *, cast_to: type, body: dict[str, object]) -> RerankResponse:
        """Record the post and return `RerankResult` rows scored by index in `order`."""
        self.posts.append((path, body))
        docs = cast("list[str]", body["documents"])
        order = self.order if self.order is not None else list(range(len(docs)))
        results = [RerankResult(index=i, relevance_score=float(i) + 0.5) for i in order]
        return RerankResponse(results=results)


class StubInner:
    """A chonkie-backend stand-in yielding fixed spans so the wrapper's strip and drop are exact.

    texts: the raw span texts the fake backend emits, whitespace and empties included.
    """

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts

    def chunk(self, text: str) -> list[SimpleNamespace]:
        """Return one `.text`-carrying span per configured raw text, ignoring the input."""
        return [SimpleNamespace(text=raw) for raw in self.texts]


def real_embedder(client: FakeEmbedClient | None = None) -> Embedder:
    """Build a genuine, uncached `Embedder`, optionally swapping the client seam for a fake.

    `object.__new__` sidesteps the `patos` singleton cache so each build reads the current
    settings fresh, then the real `__init__` threads them, and only the external client is
    replaced.

    client: the fake to install behind the seam, or None to keep the real (unused) client.
    """
    embedder = object.__new__(Embedder)
    Embedder.__init__(embedder)
    if client is not None:
        embedder.client = cast(AsyncOpenAI, client)
    return embedder


def real_reranker(client: FakeRerankClient | None = None) -> Reranker:
    """Build a genuine, uncached `Reranker`, optionally swapping the client seam for a fake.

    client: the fake to install behind the seam, or None to keep the real (unused) client.
    """
    reranker = object.__new__(Reranker)
    Reranker.__init__(reranker)
    if client is not None:
        reranker.client = cast(AsyncOpenAI, client)
    return reranker


@given(texts=st.lists(short_text, max_size=6), instruction=short_text)
def test_instructed_wraps_each_text_and_empty_is_identity(
    texts: list[str], instruction: str
) -> None:
    """An empty instruction leaves every text plain; a non-empty one wraps each in the prefix."""
    assert instructed(texts, "") == list(texts)
    wrapped = instructed(texts, instruction)
    assert wrapped == [f"Instruct: {instruction}\nQuery: {text}" for text in texts]


@given(embed_dim=st.integers(min_value=1, max_value=6), texts=st.lists(short_text, min_size=1))
def test_embed_batches_realigns_and_carries_the_width(
    monkeypatch: pytest.MonkeyPatch, embed_dim: int, texts: list[str]
) -> None:
    """Every batch carries dimensions==embed_dim and the rows realign to the input order.

    The fake returns each batch's rows reversed by index, so vectors back in 0..n order per batch
    prove the embedder re-sorts, and the plain-text inputs prove the default document lane sends
    the raw text untouched.
    """
    monkeypatch.setattr(settings, "embed_model", "test-embed")
    monkeypatch.setattr(settings, "embed_dim", embed_dim)
    monkeypatch.setattr(settings, "embed_batch_size", 3)
    monkeypatch.setattr(settings, "embed_instruction_document", "")
    client = FakeEmbedClient()
    embedder = real_embedder(client)

    vectors = asyncio.run(embedder.embed(texts))

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
    """A query embed wraps each text in the query instruction's Instruct/Query prefix."""
    monkeypatch.setattr(settings, "embed_model", "test-embed")
    monkeypatch.setattr(settings, "embed_dim", 2)
    monkeypatch.setattr(settings, "embed_instruction_query", "retrieve relevant passages")
    client = FakeEmbedClient()
    embedder = real_embedder(client)

    asyncio.run(embedder.embed(["find the fact"], mode="query"))

    assert client.embeddings.calls[0].input == [
        "Instruct: retrieve relevant passages\nQuery: find the fact"
    ]


@pytest.mark.parametrize(
    "value",
    ["http://pics.test/cat.png", "https://pics.test/cat.png", "data:image/png;base64,AAAA"],
)
def test_image_url_for_passes_urls_and_data_uris_through(value: str) -> None:
    """A url or an already-formed data URI rides untouched, never re-read or re-encoded."""
    assert image_url_for(value) == value


def test_image_url_for_encodes_paths_and_pil_images(tmp_path: Path) -> None:
    """A path with a known type, an unknown-type path, and a PIL image all inline as a data URI.

    The known suffix drives the mime from `guess_type`, the unknown one falls back to image/png,
    and the PIL branch saves through the in-memory PNG buffer.
    """
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
    """The image lane POSTs a chat body with the image_url, width, and document instruction."""
    monkeypatch.setattr(settings, "embed_model", "img-embed")
    monkeypatch.setattr(settings, "embed_dim", 2)
    monkeypatch.setattr(settings, "embed_instruction_document", "represent the document")
    client = FakeEmbedClient()
    embedder = real_embedder(client)

    [vector] = asyncio.run(embedder.embed_images(["https://pics.test/cat.png"]))

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


@pytest.mark.parametrize("api_key", ["secret", ""])
def test_embedder_threads_settings_with_and_without_a_key(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """`Embedder()` threads the live url, model, width, and key, both key header branches."""
    monkeypatch.setattr(settings, "embed_url", "http://carry.test/v1")
    monkeypatch.setattr(settings, "embed_model", "carry-embed")
    monkeypatch.setattr(settings, "embed_dim", 2)
    monkeypatch.setattr(settings, "embed_api_key", api_key)
    embedder = real_embedder()

    assert (embedder.embed_url, embedder.embed_model, embedder.embed_dim, embedder.api_key) == (
        "http://carry.test/v1",
        "carry-embed",
        2,
        api_key,
    )


@given(
    query=short_text,
    candidates=st.lists(short_text, min_size=1, max_size=6),
    data=st.data(),
)
def test_rerank_posts_the_cohere_shape_and_realigns_by_index(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    candidates: list[str],
    data: st.DataObject,
) -> None:
    """The reranker POSTs the served model, query, and documents, then realigns scores by index.

    The fake returns the results in an arbitrary permutation, so a score list keyed to the index
    proves the reranker re-seats each score at its candidate's position, not the array order.
    """
    monkeypatch.setattr(settings, "rerank_model", "bge-reranker")
    order = data.draw(st.permutations(range(len(candidates))))
    client = FakeRerankClient(list(order))
    reranker = real_reranker(client)

    scores = asyncio.run(reranker.rerank(query, candidates))

    assert scores == [float(i) + 0.5 for i in range(len(candidates))]
    path, body = client.posts[0]
    assert path == "/rerank"
    assert body == {"model": "bge-reranker", "query": query, "documents": candidates}


def test_rerank_short_circuits_with_no_candidates() -> None:
    """An empty candidate list returns no scores and makes no post, the short-circuit."""
    client = FakeRerankClient()
    reranker = real_reranker(client)

    assert asyncio.run(reranker.rerank("q", [])) == []
    assert client.posts == []


@pytest.mark.parametrize("api_key", ["secret", ""])
def test_reranker_threads_settings_with_and_without_a_key(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """`Reranker()` threads the live url, model, and key, both header branches of the key."""
    monkeypatch.setattr(settings, "rerank_url", "http://carry.test/v1")
    monkeypatch.setattr(settings, "rerank_model", "carry-reranker")
    monkeypatch.setattr(settings, "rerank_api_key", api_key)
    reranker = real_reranker()

    assert (reranker.rerank_url, reranker.rerank_model, reranker.api_key) == (
        "http://carry.test/v1",
        "carry-reranker",
        api_key,
    )


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
    """A language tag outside the denylist is code; markup, data, and unknown suffixes are not."""
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
    """Code and markup tag as text; a binary or unrecognized format does not."""
    assert is_text(Path(name)) is expected


def test_file_tags_sniffs_content_of_a_real_file(tmp_path: Path) -> None:
    """A real, extensionless file is tagged by its shebang, the content-sniff `exists` branch.

    The filename alone resolves no language, so a python tag can only come from `tags_from_path`
    reading the executable shebang, the lever `tags_from_filename` cannot offer.
    """
    script = tmp_path / "script"
    script.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    script.chmod(0o755)

    assert file_tags(Path(script.name)) == file_tags(tmp_path / "missing")
    assert "python" in file_tags(script)
    assert is_text(script)
    assert is_code(script)


@pytest.mark.parametrize("kind", [ChonkieChunker, CodeChunker])
def test_chunk_strips_and_drops_empty_spans(
    kind: type[ChonkieChunker | CodeChunker],
) -> None:
    """Both chunkers strip each span and drop the empties, preserving order.

    The backend is stubbed so the whitespace-only and empty spans are guaranteed, exercising the
    wrapper's drop branch the real backend rarely emits; the real construction is covered by the
    live-backend test below.
    """
    chunker = object.__new__(kind)
    object.__setattr__(chunker, "chunker", StubInner(["  a  ", "   ", "", "b\n", "\tc"]))

    assert chunker.chunk("ignored") == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("build", "text", "needle"),
    [
        (
            ChonkieChunker,
            "The Leech lattice packs spheres.\n\nThe Conway group is sporadic.",
            "Leech",
        ),
        (CodeChunker, "def a(x):\n    return x + 1\n\n\ndef b(y):\n    return y * 2\n", "def a"),
    ],
)
def test_chunk_real_backend_returns_clean_spans(
    build: type[ChonkieChunker | CodeChunker], text: str, needle: str
) -> None:
    """The real chonkie prose and code backends split a sample into clean, non-empty spans."""
    spans = build().chunk(text)

    assert spans
    assert all(span == span.strip() and span for span in spans)
    assert any(needle in span for span in spans)
