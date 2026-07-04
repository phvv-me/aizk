import re

from slugify import slugify

# wikilink and markdown link wrappers stripped before anything else, so [[Team Memory]] and
# [Graphiti](https://x) reduce to their visible label rather than minting a node named after the
# syntax. The markdown form keeps the link text and drops the target in parentheses.
WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")

# a name that is really a filesystem path or a url, the slug failure mode where extraction echoed a
# source locator instead of a thing. Such a name has no place in the graph, so it folds to empty
# and the caller drops it rather than interning a path as an entity. Checked before slugify runs,
# since slugify has no notion of a path and would otherwise happily fold one into a fake entity.
PATH_LIKE = re.compile(r"^(?:[a-z]+://|/|\.{0,2}/|[~.]/)|/")


def normalize_name(name: str) -> str:
    """Fold an entity name to a canonical key, collapsing slugs and stripping link and path forms.

    Unwraps wikilink and markdown link syntax to the visible label, returns empty for a name that
    is a filesystem path or url so the caller drops it, then delegates the fold to
    python-slugify's transliterating, space-separated slug so team-memory-spine, Team Memory
    Spine, Café, and [[Team Memory Spine]] all reduce to one stable ascii key while
    notes/graph_rag.md folds to nothing.

    Case-folds with `str.casefold()` before handing off to slugify, since slugify's own
    transliteration table is not casefold-consistent across scripts (Georgian Mtavruli and
    Mkhedruli letters transliterate to different Latin output for what `str.casefold()` treats as
    the same letter). Folding first makes the two forms converge before slugify ever sees them.

    name: raw entity surface form proposed by extraction.
    """
    unwrapped = MARKDOWN_LINK.sub(r"\1", WIKILINK.sub(r"\1", name)).strip()
    if PATH_LIKE.search(unwrapped):
        return ""
    return slugify(unwrapped.casefold(), separator=" ")
