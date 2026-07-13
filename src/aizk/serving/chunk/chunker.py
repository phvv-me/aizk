from pathlib import Path

from identify import identify

from ...config import settings

# the structural tags `identify` mixes into every result, describing the filesystem entry rather
# than its content, so they never count as the "language tag" is_code looks for.
_GENERIC_TAGS = identify.TYPE_TAGS | identify.MODE_TAGS | identify.ENCODING_TAGS


def file_tags(path: Path) -> frozenset[str]:
    """The `identify` tags a path carries, sniffing content when a real file exists on disk."""
    return frozenset(
        identify.tags_from_path(str(path))
        if path.exists()
        else identify.tags_from_filename(path.name)
    )


def is_text(path: Path) -> bool:
    """Whether a path names a file `identify` considers text, the ingest directory-walk
    filter."""
    return "text" in file_tags(path)


def is_code(path: Path) -> bool:
    """Whether a path names a source file the code chunker should handle."""
    languages = file_tags(path) - _GENERIC_TAGS
    return bool(languages) and languages.isdisjoint(settings.chunk_denylist_languages)
