from functools import cache

from chonkie import RecursiveChunker

from ...config import settings


@cache
def _chunker(chunk_size: int) -> RecursiveChunker:
    """Reuse each configured prose chunker."""
    return RecursiveChunker(chunk_size=chunk_size)


def chunk_text(text: str, chunk_size: int = settings.chunk_size) -> list[str]:
    """Split prose recursively and return trimmed, nonempty spans."""
    spans = (span.text.strip() for span in _chunker(chunk_size).chunk(text))
    return [span for span in spans if span]
