import re

from slugify.slugify import slugify

# Preserve visible labels while removing link syntax.
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")

_PATH_LIKE = re.compile(r"^(?:[a-z]+://|/|\.{0,2}/|[~.]/)|/")


def normalize_name(name: str) -> str:
    """Fold an entity name to a canonical key, collapsing slugs and stripping link and path
    forms."""
    unwrapped = _MARKDOWN_LINK.sub(r"\1", _WIKILINK.sub(r"\1", name)).strip()
    if _PATH_LIKE.search(unwrapped):
        return ""
    return slugify(unwrapped.casefold(), separator=" ")
