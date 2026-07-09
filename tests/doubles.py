import hashlib
from dataclasses import dataclass, field
from typing import cast

from patos import SingletonMeta
from pydantic import BaseModel

from aizk.config import settings
from aizk.extract import ontology
from aizk.extract.models import BatchConsolidationVerdict, ConsolidationVerdict
from aizk.graph.models import (
    CommunitySummary,
    InsightReport,
    ProfileReport,
    RaptorReport,
)
from aizk.serving.embed import Embedder, EmbedMode
from aizk.serving.rerank import Reranker


def deterministic_vector(text: str, dim: int) -> list[float]:
    """A fixed-width vector depending only on the text, so a recall is reproducible run to run.

    text: the string being embedded.
    dim: the width every returned vector carries, the halfvec dimension by default.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [digest[index % len(digest)] / 255.0 for index in range(dim)]


class UncachedMeta(SingletonMeta):
    """Metaclass for a `Singleton` subclass that builds fresh on every construction.

    `Embedder` and `Reranker` are `patos` singletons, one shared instance per class forever after
    the first construction. A recording double subclasses one only for its `isinstance` contract,
    never for the caching, so overriding `__call__` back to plain `type.__call__` opts the double
    out of the cache while staying, through the hierarchy, an instance of the real class.
    """

    def __call__(cls: type, *args: object, **kwargs: object) -> object:
        return type.__call__(cls, *args, **kwargs)


class RecordingEmbedder(Embedder, metaclass=UncachedMeta):
    """A recording double for the embedder seam, deterministic and free of any model or network.

    Subclasses `Embedder` only so it type-checks everywhere a real one is expected, never calling
    the base `__init__` and so never building the `AsyncOpenAI` client it would. Records every text
    and image call and returns fixed-width vectors from the input, so two embeds of one text match
    the way a real embedder's cosine ranking depends on.

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

    Subclasses `Reranker` only for its `isinstance` contract, never building the client. Scores
    each candidate by its shared character overlap with the query, a monotone stand-in for a
    cross-encoder that needs no model, so a test asserts the reorder without a GPU.
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

    Writes the double onto `Embedder.singleton_instance`, the slot `SingletonMeta.__call__` hands
    back with no re-run of `__init__`, so every caller's `Embedder()` resolves to it until the slot
    is cleared. A `RuleBasedStateMachine` that cannot request the fixture calls this directly.

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


def default_response(schema: type[BaseModel]) -> BaseModel:
    """A minimal valid instance of one extractor or summarizer schema, the fake LLM's fallback.

    Keyed by `schema.__name__` rather than the class object itself, since the combined
    extraction call's wire schema (`ontology.current().llm_extraction`) is rebuilt fresh from
    the live catalog on every `ontology.refresh`, a different class object each time under the
    same name, so identity is not a stable key across a test's lifetime the way it is for every
    other, statically defined schema here.

    schema: the response model the seam asked the LLM for.
    """
    defaults: dict[str, BaseModel] = {
        "LLMExtraction": ontology.current().llm_extraction(e=[], f=[]),
        BatchConsolidationVerdict.__name__: BatchConsolidationVerdict(verdicts=[]),
        ConsolidationVerdict.__name__: ConsolidationVerdict(action="ADD"),
        CommunitySummary.__name__: CommunitySummary(
            label="cluster theme", summary="a grounded paragraph"
        ),
        ProfileReport.__name__: ProfileReport(summary="a static and dynamic paragraph"),
        RaptorReport.__name__: RaptorReport(label="broad theme", summary="a rolled-up paragraph"),
        InsightReport.__name__: InsightReport(observations=[]),
    }
    return defaults[schema.__name__]


@dataclass
class FakeMessage:
    """The `.choices[0].message` shape `structured` reads its `.parsed` schema instance off of."""

    parsed: BaseModel


@dataclass
class FakeChoice:
    """The `.choices[0]` shape wrapping the fake message, mirroring `ParsedChoice`."""

    message: FakeMessage


@dataclass
class FakeParsedCompletion:
    """A minimal stand-in for `openai.types.chat.ParsedChatCompletion`, only `.choices` read."""

    choices: list[FakeChoice]


class FakeCompletions:
    """A recording completions stand-in dispatching on the requested response_format schema.

    Returns the canned instance a test registered for a schema, or a minimal valid default, so any
    summarizer and extractor that flows through `structured` runs without the local model. This
    replaces the one external LLM process at its seam, never any of our own classes.

    responses: per-schema-name overrides the test installs, falling back to a minimal valid
        default. Keyed by name rather than the class object, see `default_response`.
    calls: every turn's kwargs, normalized to `response_model`/`messages` keys.
    """

    def __init__(self) -> None:
        self.responses: dict[str, BaseModel] = {}
        self.calls: list[dict[str, object]] = []

    async def parse(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: type[BaseModel],
        temperature: float | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, object] | None = None,
    ) -> FakeParsedCompletion:
        """Record the turn and return the canned or default model for its response_format schema.

        model: chat model id the seam sent.
        messages: the system-then-user message pair the seam assembled.
        response_format: schema the caller asked the structured turn to validate against.
        temperature: sampling temperature, accepted and ignored.
        timeout: per-call ceiling, accepted and ignored.
        max_tokens: output token cap, accepted and ignored.
        extra_body: provider extra_body (chat_template_kwargs), recorded, accepted and ignored.
        """
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "response_model": response_format,
                "temperature": temperature,
                "timeout": timeout,
                "max_tokens": max_tokens,
                "extra_body": extra_body,
            }
        )
        parsed = self.responses.get(response_format.__name__) or default_response(response_format)
        return FakeParsedCompletion(choices=[FakeChoice(FakeMessage(parsed))])


@dataclass
class FakeChat:
    """The chat namespace wrapping the fake completions."""

    completions: FakeCompletions


class FakeLLM:
    """An AsyncOpenAI stand-in exposing only the `chat.completions.parse` path the seam uses.

    completions: the recording completions the chat namespace exposes.
    """

    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = FakeChat(self.completions)

    def register(self, schema: type[BaseModel], response: BaseModel) -> None:
        """Pin the parsed instance the fake returns for one response schema.

        schema: the response_format a `structured` call will ask for, matched by name, see
            `default_response`.
        response: the instance to hand back for that schema.
        """
        self.completions.responses[schema.__name__] = response


@dataclass
class FakeJob:
    """The one attribute the queue bodies read off a dequeued job, its encoded payload."""

    payload: bytes = b"{}"


@dataclass
class RecordingEnqueue:
    """One recorded enqueue, the unit the fan-out and the on-write chain emit."""

    entrypoint: str
    payload: bytes
    dedupe_key: str | None = field(default=None)
