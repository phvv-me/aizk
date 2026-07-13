import re


def normalized_map(text: str) -> tuple[str, list[int]]:
    """Casefold text with whitespace runs collapsed, keeping each position's source offset.

    text: the string to normalize.
    """
    folded: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for offset, char in enumerate(text):
        if char.isspace():
            pending_space = bool(folded)
            continue
        if pending_space:
            folded.append(" ")
            offsets.append(offset - 1)
            pending_space = False
        # One offset per folded character: a single source char may casefold to several.
        for piece in char.casefold():
            folded.append(piece)
            offsets.append(offset)
    return "".join(folded), offsets


def quote_interval(quote: str | None, text: str) -> tuple[int, int] | None:
    """Locate a model-emitted quote in its source text as `(start, end)` offsets.

    An exact match wins; otherwise matching retries case- and whitespace-insensitively, the
    two ways a model most often mangles a "verbatim" excerpt. A quote that still cannot be
    found returns None and the fact simply carries no grounding.

    quote: the excerpt the extractor claims is verbatim.
    text: the chunk text the excerpt should appear in.
    """
    if quote is None or not (quote := quote.strip()):
        return None
    start = text.find(quote)
    if start >= 0:
        return start, start + len(quote)
    folded_text, offsets = normalized_map(text)
    folded_quote = re.sub(r"\s+", " ", quote.casefold()).strip()
    start = folded_text.find(folded_quote)
    if start < 0:
        return None
    last = offsets[start + len(folded_quote) - 1]
    return offsets[start], last + 1
