from patos import Singleton

from ...config import settings


class CodeChunker(Singleton):
    """The single code chunker, backed by chonkie's CodeChunker for source files.

    The code splitter parses the file with tree-sitter and breaks on whole syntactic units, a
    function or class body, so a span never straddles a definition. Language is left on auto so the
    splitter infers it from the content, which keeps one backend serving every supported language.
    A `patos` singleton, so settings.chunk_size is read once, on the first `CodeChunker()`
    construction.

    chunk_size: target characters per span handed to the code splitter.
    """

    def __init__(self) -> None:
        from chonkie import CodeChunker as ChonkieCodeChunker

        self.chunker = ChonkieCodeChunker(chunk_size=settings.chunk_size, language="auto")

    def chunk(self, text: str) -> list[str]:
        """Split source recursively on syntactic units and return the non-empty spans.

        text: full source file text to split.
        """
        spans = (span.text.strip() for span in self.chunker.chunk(text))
        return [span for span in spans if span]
