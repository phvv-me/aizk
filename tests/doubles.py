import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from PIL.Image import Image
from pydantic import BaseModel, JsonValue

from aizk.config import settings
from aizk.extract import ontology
from aizk.extract.models import BatchConsolidationVerdict, ConsolidationVerdict
from aizk.graph.models import (
    CommunitySummary,
    InsightReport,
    ProfileReport,
    RaptorReport,
)
from aizk.serving.embed import EmbedMode


def deterministic_vector(text: str, dim: int) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [digest[index % len(digest)] / 255.0 for index in range(dim)]


class RecordingEmbedder:
    def __init__(self, dim: int = settings.embed_dim) -> None:
        self.embed_url, self.embed_model, self.embed_dim = "fake://embed.test/v1", "fake", dim
        self.calls: list[tuple[list[str], str]] = []
        self.image_calls: list[list[str]] = []

    async def embed(self, texts: list[str], mode: EmbedMode = "document") -> list[list[float]]:
        self.calls.append((list(texts), mode))
        return [deterministic_vector(f"{mode}:{text}", self.embed_dim) for text in texts]

    async def embed_images(self, images: list[str | Image]) -> list[list[float]]:
        self.image_calls.append([str(image) for image in images])
        return [deterministic_vector(f"image:{image}", self.embed_dim) for image in images]


def default_response(schema: type[BaseModel]) -> BaseModel:
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
    parsed: BaseModel | None


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeParsedCompletion:
    choices: list[FakeChoice]


@dataclass(frozen=True)
class CompletionCall:
    model: str
    messages: list[dict[str, str]]
    response_model: type[BaseModel]
    temperature: float | None
    timeout: float | None
    max_tokens: int | None
    extra_body: dict[str, JsonValue] | None


class CompletionParser(Protocol):
    async def parse(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: type[BaseModel],
        temperature: float | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, JsonValue] | None = None,
    ) -> FakeParsedCompletion: ...


class FakeCompletions:
    def __init__(self) -> None:
        self.responses: dict[str, BaseModel] = {}
        self.calls: list[CompletionCall] = []

    async def parse(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: type[BaseModel],
        temperature: float | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, JsonValue] | None = None,
    ) -> FakeParsedCompletion:
        self.calls.append(
            CompletionCall(
                model=model,
                messages=messages,
                response_model=response_format,
                temperature=temperature,
                timeout=timeout,
                max_tokens=max_tokens,
                extra_body=extra_body,
            )
        )
        parsed = self.responses.get(response_format.__name__) or default_response(response_format)
        return FakeParsedCompletion(choices=[FakeChoice(FakeMessage(parsed))])


@dataclass
class FakeChat:
    completions: CompletionParser


class FakeLLM:
    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = FakeChat(self.completions)

    def register(self, schema: type[BaseModel], response: BaseModel) -> None:
        self.completions.responses[schema.__name__] = response


@dataclass
class FakeJob:
    payload: bytes = b"{}"


@dataclass
class RecordingEnqueue:
    entrypoint: str
    payload: bytes
    dedupe_key: str | None = field(default=None)
