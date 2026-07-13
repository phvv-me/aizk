from functools import cache

from chonkie import RecursiveChunker

from ...config import settings


@cache
def _chunker() -> RecursiveChunker:
    """Reuse the configured prose chunker."""
    return RecursiveChunker(chunk_size=settings.chunk_size)


def chunk_text(text: str) -> list[str]:
    """Split prose recursively and return trimmed, nonempty spans."""
    spans = (span.text.strip() for span in _chunker().chunk(text))
    return [span for span in spans if span]
