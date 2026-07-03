from pathlib import Path
from typing import Protocol, runtime_checkable

from identify import identify

from ...config import settings

# the structural tags `identify` mixes into every result, describing the filesystem entry rather
# than its content, so they never count as the "language tag" is_code looks for.
GENERIC_TAGS = identify.TYPE_TAGS | identify.MODE_TAGS | identify.ENCODING_TAGS


@runtime_checkable
class Chunker(Protocol):
    """A backend that splits a document into retrieval-sized text spans."""

    def chunk(self, text: str) -> list[str]:
        """Split text into ordered, non-empty spans.

        text: full document text to split.
        """
        ...


def file_tags(path: Path) -> frozenset[str]:
    """The `identify` tags a path carries, sniffing content when a real file exists on disk.

    `identify.tags_from_path` peeks at the shebang or the byte content itself when the filename
    alone resolves no tag, the lever a bare `tags_from_filename` cannot offer, so it is preferred
    whenever the path names a file this process can stat. A path that names no real file, such as
    one about to be written during a directory walk's dry probe, falls back to the pure filename
    resolver.

    path: file path to tag.
    """
    if path.exists():
        return frozenset(identify.tags_from_path(str(path)))
    return frozenset(identify.tags_from_filename(path.name))


def is_text(path: Path) -> bool:
    """Whether a path names a file `identify` considers text, the ingest directory-walk filter.

    path: candidate file the directory walk is filtering.
    """
    return "text" in file_tags(path)


def is_code(path: Path) -> bool:
    """Whether a path names a source file the code chunker should handle.

    A path carries a language tag, one of `identify`'s tags beyond the generic structural ones
    `GENERIC_TAGS` names, that is not in `settings.chunk_denylist_languages`. A recognized markup
    or data-serialization format is excluded by the denylist since it parses fine but carries no
    function or class body the code chunker exists to protect.

    path: file path whose tags decide the chunker lane.
    """
    languages = file_tags(path) - GENERIC_TAGS
    return bool(languages) and languages.isdisjoint(settings.chunk_denylist_languages)
