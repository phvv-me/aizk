import hashlib
from typing import cast

from patos import SingletonMeta

from aizk.config import settings
from aizk.serving.embed import Embedder, EmbedMode
from aizk.serving.rerank import Reranker


def deterministic_vector(text: str, dim: int) -> list[float]:
    """A fixed-width vector that depends only on the text, so a recall is reproducible run to run.

    text: the string being embedded.
    dim: the width every returned vector carries, the halfvec dimension by default.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [digest[index % len(digest)] / 255.0 for index in range(dim)]


class UncachedMeta(SingletonMeta):
    """Metaclass for a `Singleton` subclass that must build fresh on every construction.

    `Embedder` and `Reranker` are `patos` singletons, one shared instance per class forever after
    the first construction, exactly the caching production code wants. A recording test double
    subclasses one of them purely for its `isinstance` contract, since callers type-hint the real
    class, never for the caching, since a fresh double with an empty `calls` list is what every
    test needs. Overriding `__call__` back to plain `type.__call__` opts a double class out of
    `SingletonMeta`'s cache while it stays, through the class hierarchy, built by the pattern's own
    metaclass, so a `RecordingEmbedder()` in one test never replays another test's stale instance.
    """

    def __call__(cls: type, *args: object, **kwargs: object) -> object:
        return type.__call__(cls, *args, **kwargs)


class RecordingEmbedder(Embedder, metaclass=UncachedMeta):
    """A recording double for the embedder seam, deterministic and free of any model or network.

    Subclasses `Embedder` only so it type-checks everywhere a real one is expected, never calling
    the base `__init__` and so never building the `AsyncOpenAI` client it would. It records every
    text and image call so a test can assert what the code under test passed, and returns
    fixed-width vectors derived from the input, so two embeds of the same text or image match the
    way a real embedder's cosine ranking depends on. This stands in for the external vLLM process
    at the one seam, and exposes both the text and image lanes since production `Embedder` does.

    dim: width of every returned vector, the halfvec dimension by default.
    """

    def __init__(self, dim: int = settings.embed_dim) -> None:
        self.embed_url, self.embed_model, self.embed_dim = "fake://embed.test/v1", "fake", dim
        self.calls: list[tuple[list[str], str]] = []
        self.image_calls: list[list[str]] = []

    async def embed(self, texts: list[str], mode: EmbedMode = "document") -> list[list[float]]:
        """Record the call and return one deterministic vector per text.

        texts: input strings to embed.
        mode: query or document, recorded so a test can assert the lane the caller chose.
        """
        self.calls.append((list(texts), mode))
        return [deterministic_vector(f"{mode}:{text}", self.embed_dim) for text in texts]

    async def embed_images(self, images: list[str]) -> list[list[float]]:
        """Record the call and return one deterministic vector per image reference.

        images: file paths, urls, or data URIs to embed.
        """
        self.image_calls.append([str(image) for image in images])
        return [deterministic_vector(f"image:{image}", self.embed_dim) for image in images]


class RecordingReranker(Reranker, metaclass=UncachedMeta):
    """A recording double for the reranker seam, scoring by a fixed, query-aware rule.

    Subclasses `Reranker` only so it type-checks everywhere a real one is expected, never calling
    the base `__init__` and so never building the `AsyncOpenAI` client it would. It records the
    query and candidates each call carried and scores each candidate by its shared character
    overlap with the query, a monotone stand-in for a cross-encoder that needs no model, so a test
    can assert the reorder without a GPU. This replaces the external rerank process.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        """Record the call and score each candidate by its character overlap with the query.

        query: the search string.
        candidates: candidate texts row-aligned with the returned scores.
        """
        self.calls.append((query, list(candidates)))
        terms = set(query)
        return [float(len(terms & set(candidate))) for candidate in candidates]


def install_fake_embedder(embedder: RecordingEmbedder | None) -> RecordingEmbedder | None:
    """Swap every `Embedder()` call onto a fixed double, or restore real construction when None.

    Every module reads the shared instance through `Embedder()`, and `SingletonMeta.__call__` hands
    back whatever object lives at `Embedder`'s own `singleton_instance` class attribute without
    re-running `__init__`. `RecordingEmbedder` subclasses `Embedder`, so writing it directly onto
    that slot with `setattr` (a class's `__dict__` is a read-only mappingproxy, so `setattr` and
    `delattr` are the only way to write it) redirects every caller's `Embedder()` onto the double
    for the duration of one test, restoring by clearing the slot so the next real `Embedder()`
    builds fresh from `settings`. A `RuleBasedStateMachine` is a unittest case that cannot request
    the `fake_embedder` fixture, so it installs and clears the recording double here directly in
    its initialize and teardown.

    embedder: the recording double to install, or None to restore real construction.
    """
    previous = cast("RecordingEmbedder | None", Embedder.__dict__.get("singleton_instance"))
    if embedder is None:
        if "singleton_instance" in Embedder.__dict__:
            delattr(Embedder, "singleton_instance")
    else:
        Embedder.singleton_instance = embedder
    return previous


def install_fake_reranker(reranker: RecordingReranker | None) -> RecordingReranker | None:
    """Swap every `Reranker()` call onto a fixed double, or restore real construction when None.

    reranker: the recording double to install, or None to restore real construction.
    """
    previous = cast("RecordingReranker | None", Reranker.__dict__.get("singleton_instance"))
    if reranker is None:
        if "singleton_instance" in Reranker.__dict__:
            delattr(Reranker, "singleton_instance")
    else:
        Reranker.singleton_instance = reranker
    return previous
