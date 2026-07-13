from functools import cache

from chonkie import CodeChunker as ChonkieCodeChunker

from ...config import settings


@cache
def _chunker() -> ChonkieCodeChunker:
    """Reuse the configured source chunker."""
    return ChonkieCodeChunker(chunk_size=settings.chunk_size, language="auto")


def chunk_code(text: str) -> list[str]:
    """Split source on syntactic units and return trimmed, nonempty spans."""
    spans = (span.text.strip() for span in _chunker().chunk(text))
    return [span for span in spans if span]
