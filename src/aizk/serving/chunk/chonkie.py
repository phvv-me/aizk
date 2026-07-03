from patos import Singleton

from ...config import settings


class ChonkieChunker(Singleton):
    """The single prose chunker, backed by chonkie's RecursiveChunker.

    The recursive splitter walks a ladder of delimiters, paragraphs then sentences then
    punctuation, keeping each span near the size budget while respecting natural boundaries,
    which gives cleaner inputs to extraction than blind packing. A `patos` singleton, so
    settings.chunk_size is read once, on the first `ChonkieChunker()` construction.

    chunk_size: target characters per span handed to RecursiveChunker.
    """

    def __init__(self) -> None:
        from chonkie import RecursiveChunker

        self.chunker = RecursiveChunker(chunk_size=settings.chunk_size)

    def chunk(self, text: str) -> list[str]:
        """Split text recursively and return the trimmed, non-empty spans.

        text: full document text to split.
        """
        spans = (span.text.strip() for span in self.chunker.chunk(text))
        return [span for span in spans if span]
