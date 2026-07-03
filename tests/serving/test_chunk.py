from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.config import settings
from aizk.serving.chunk import ChonkieChunker, Chunker, CodeChunker, is_code, is_text

chunk_text = ChonkieChunker().chunk

word = st.from_regex(r"[a-z]{1,10}", fullmatch=True)
paragraph = st.lists(word, min_size=1, max_size=8)
paragraphs = st.lists(paragraph, min_size=1, max_size=6)

# one representative suffix per language family, standing in for the hundreds `identify` resolves
# without us enumerating them ourselves
CODE_SUFFIXES = (
    ".py",
    ".rs",
    ".go",
    ".ts",
    ".tsx",
    ".java",
    ".c",
    ".cpp",
    ".rb",
    ".sh",
    ".sql",
)

# formats `identify` still tags but that carry no function or class body, the
# settings.chunk_denylist denylist exists to keep in the prose lane, plus an unrecognized suffix
# `identify` tags no language at all for
NOT_CODE_SUFFIXES = (".md", ".html", ".json", ".yaml", ".toml", ".csv", ".txt", ".bin")


@given(text=st.text())
def test_no_empty_chunks(text: str) -> None:
    """Every chunk is non-empty after stripping whitespace, for any input."""
    assert all(chunk.strip() for chunk in chunk_text(text))


@given(blocks=paragraphs)
def test_all_paragraphs_covered(blocks: list[list[str]]) -> None:
    """Every word of every paragraph survives into some chunk."""
    text = "\n\n".join(" ".join(block) for block in blocks)
    produced = {token for chunk in chunk_text(text) for token in chunk.split()}
    expected = {token for block in blocks for token in block}
    assert expected <= produced


@given(blocks=paragraphs)
def test_chunks_are_stripped_strings(blocks: list[list[str]]) -> None:
    """Chunking real prose yields a list of clean, non-empty strings from a Chunker."""
    text = "\n\n".join(" ".join(block) for block in blocks)
    chunks = chunk_text(text)
    assert isinstance(ChonkieChunker(), Chunker)
    assert all(isinstance(chunk, str) and chunk.strip() for chunk in chunks)


@pytest.mark.parametrize("suffix", CODE_SUFFIXES)
def test_is_code_admits_a_representative_suffix_per_language_either_case(suffix: str) -> None:
    """A representative programming-language suffix routes to the code lane, case-folded too."""
    assert is_code(Path(f"module{suffix}"))
    assert is_code(Path(f"module{suffix.upper()}"))


@pytest.mark.parametrize("name", [f"note{suffix}" for suffix in NOT_CODE_SUFFIXES])
def test_is_code_rejects_markup_and_data_suffixes_despite_an_identify_tag(name: str) -> None:
    """A markup or data-serialization format is not code, even though `identify` tags it too."""
    assert not is_code(Path(name))


def test_is_code_rejects_an_unrecognized_suffix() -> None:
    """A suffix `identify` has never heard of falls to the prose lane."""
    assert not is_code(Path("module.definitely-not-a-real-suffix"))


@pytest.mark.parametrize("suffix", CODE_SUFFIXES + (".md",))
def test_is_text_admits_code_and_markdown(suffix: str) -> None:
    """Every code suffix and markdown, the two ingest_path lanes, tag as text."""
    assert is_text(Path(f"module{suffix}"))


@pytest.mark.parametrize("name", ["scan.pdf", "report.docx", "module.bin"])
def test_is_text_rejects_binary_and_unrecognized_formats(name: str) -> None:
    """A binary document format or an unrecognized suffix carries no text tag."""
    assert not is_text(Path(name))


def test_chonkie_and_code_chunkers_are_each_a_singleton() -> None:
    """`ChonkieChunker()` and `CodeChunker()` are `patos` singletons, one shared instance each."""
    assert ChonkieChunker() is ChonkieChunker()
    assert CodeChunker() is CodeChunker()
    assert ChonkieChunker() is not CodeChunker()


def test_is_code_selects_the_chunker_class_at_the_call_site() -> None:
    """The `CodeChunker() if is_code(path) else ChonkieChunker()` pattern ingest.py dispatches with
    picks the code chunker for a source path and the prose chunker for everything else.
    """
    code_path, prose_path = Path("module.py"), Path("note.md")
    assert isinstance(CodeChunker() if is_code(code_path) else ChonkieChunker(), CodeChunker)
    assert isinstance(CodeChunker() if is_code(prose_path) else ChonkieChunker(), ChonkieChunker)


SAMPLE_PROSE = (
    "The Leech lattice packs spheres optimally in twenty four dimensions.\n\n"
    "Its automorphism group is the Conway group, one of the sporadic simple groups.\n\n"
    "Quantization onto that lattice underlies the codec studied in this note."
)
SAMPLE_CODE = (
    "def alpha(x):\n    return x + 1\n\n\n"
    "def beta(y):\n    return y * 2\n\n\n"
    "class Gamma:\n    def method(self):\n        return alpha(beta(3))\n"
)


def chunker_with_size(kind: type[ChonkieChunker | CodeChunker], chunk_size: int) -> Chunker:
    """Build a chonkie-backed singleton chunker fresh, with chunk_size overridden for the build.

    `ChonkieChunker`/`CodeChunker` are `patos` singletons, one shared instance per class forever
    after the first construction, so this clears the cached slot before constructing and again on
    exit, mirroring `fresh_embedder`, leaving no test-configured chunker behind for a later test's
    real construction to reuse. A manual `pytest.MonkeyPatch` stands in for the deleted `override`,
    since this helper is not itself a fixture and the constructor only reads `chunk_size`
    synchronously in its body, so restoring immediately after the build leaves no window for the
    built chunker to see it change.

    kind: ChonkieChunker or CodeChunker, whose constructor reads settings.chunk_size.
    chunk_size: value chunk_size is temporarily set to for the duration of construction.
    """
    patch = pytest.MonkeyPatch()
    patch.setattr(settings, "chunk_size", chunk_size)
    if "singleton_instance" in kind.__dict__:
        delattr(kind, "singleton_instance")
    try:
        return kind()
    finally:
        if "singleton_instance" in kind.__dict__:
            delattr(kind, "singleton_instance")
        patch.undo()


@pytest.mark.parametrize(
    ("build", "sample"),
    [
        (lambda: chunker_with_size(ChonkieChunker, 256), SAMPLE_PROSE),
        (lambda: chunker_with_size(CodeChunker, 64), SAMPLE_CODE),
    ],
)
def test_chonkie_chunkers_return_non_empty_stripped_spans(
    build: Callable[[], Chunker], sample: str
) -> None:
    """The chonkie prose and code backends split a sample into clean, non-empty spans."""
    chunker = build()
    spans = chunker.chunk(sample)
    assert isinstance(chunker, Chunker)
    assert spans
    assert all(isinstance(span, str) and span.strip() for span in spans)
