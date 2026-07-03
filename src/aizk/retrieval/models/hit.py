from patos import FrozenModel


class Hit(FrozenModel):
    """A single hybrid-search result row.

    document_title: title of the parent document when known.
    source_uri: origin locator of the parent document when known.
    text: the matched chunk text.
    score: fused relevance score, higher is better.
    """

    document_title: str | None
    source_uri: str | None
    text: str
    score: float
