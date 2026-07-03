import uuid

from patos import FrozenModel


class ChunkCandidate(FrozenModel):
    """One ranked chunk from a single retrieval lane, before cross-lane fusion.

    id: chunk identity, the key the dense and lexical lanes fuse on.
    document_title: title of the parent document when known.
    source_uri: origin locator of the parent document when known.
    text: the chunk text.
    promoted: whether the parent document carries promote provenance, the trusted-first signal
        that gives the fused chunk a small rank bonus.
    """

    id: uuid.UUID
    document_title: str | None
    source_uri: str | None
    text: str
    promoted: bool = False
