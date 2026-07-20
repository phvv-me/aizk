import hashlib
from dataclasses import dataclass, field
from types import TracebackType

from pydantic import BaseModel, JsonValue
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile

from aizk.config import settings
from aizk.extract.models import BatchConsolidationVerdict, ConsolidationVerdict
from aizk.graph.models import (
    CommunitySummary,
    InsightReport,
    ProfileReport,
    RaptorReport,
)
from aizk.ontology import WireExtraction
from aizk.serving.embed import EmbedMode
from aizk.serving.extract import LLM


class AsyncContext[ValueT]:
    """Minimal explicit async context around one test value."""

    def __init__(self, value: ValueT) -> None:
        self.value = value

    async def __aenter__(self) -> ValueT:
        return self.value

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def deterministic_vector(text: str, dim: int) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [digest[index % len(digest)] / 255.0 for index in range(dim)]


class RecordingEmbedder:
    def __init__(self, dim: int = settings.embed_dim) -> None:
        self.embed_url, self.embed_model, self.embed_dim = "fake://embed.test/v1", "fake", dim
        self.calls: list[tuple[list[str], str]] = []

    async def embed(self, texts: list[str], mode: EmbedMode = "document") -> list[list[float]]:
        self.calls.append((list(texts), mode))
        return [deterministic_vector(f"{mode}:{text}", self.embed_dim) for text in texts]


class NeutralReranker:
    """Score every rerank call neutrally and record the texts each call saw."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        del query
        self.calls.append(list(texts))
        return [0.0] * len(texts)


class NeutralGate:
    """Relevance gate that admits every chunk and seeds no mentions."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def relevant(self, text: str) -> bool:
        self.calls.append(text)
        return True

    async def named_entities(self, text: str) -> list[str]:
        del text
        return []


def default_response(schema: type[BaseModel]) -> BaseModel:
    defaults: dict[str, BaseModel] = {
        WireExtraction.__name__: WireExtraction(e=[], f=[]),
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


@dataclass(frozen=True)
class CompletionCall:
    model: str
    messages: list[dict[str, str]]
    response_model: type[BaseModel]
    temperature: float | None
    timeout: float | None
    max_tokens: int | None
    extra_body: dict[str, JsonValue] | None


class FakeCompletions:
    def __init__(self) -> None:
        self.responses: dict[str, BaseModel] = {}
        self.calls: list[CompletionCall] = []
        self.raw: str | None = None
        self.error: BaseException | None = None


class FakeLLM:
    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.model = FunctionModel(
            self.complete,
            profile=ModelProfile(
                supports_json_schema_output=True,
                default_structured_output_mode="native",
            ),
        )

    @property
    def llm(self) -> LLM:
        """A real `LLM` service running over this fake model, ready to inject."""
        return LLM(
            model=self.model,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
            response_max_tokens=settings.llm_response_max_tokens,
            chat_template_kwargs=settings.llm_chat_template_kwargs,
            extra_body=settings.llm_extra_body,
        )

    def register(self, schema: type[BaseModel], response: BaseModel) -> None:
        self.completions.responses[schema.__name__] = response

    def complete(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Return the registered typed response and retain the effective model request."""
        if self.completions.error is not None:
            raise self.completions.error
        output = info.model_request_parameters.output_object
        if output is None:
            raise ValueError("fake LLM requires native structured output")
        if output.name is None:
            raise ValueError("fake LLM requires a named output schema")
        response_model = next(
            schema
            for schema in (
                WireExtraction,
                BatchConsolidationVerdict,
                ConsolidationVerdict,
                CommunitySummary,
                ProfileReport,
                RaptorReport,
                InsightReport,
            )
            if schema.__name__ == output.name
        )
        model_settings = info.model_settings or {}
        extra_body = model_settings.get("extra_body")
        timeout = model_settings.get("timeout")
        request = next(
            message
            for message in reversed(messages)
            if isinstance(message, ModelRequest)
            and any(isinstance(part, UserPromptPart) for part in message.parts)
        )
        user = next(part for part in request.parts if isinstance(part, UserPromptPart))
        self.completions.calls.append(
            CompletionCall(
                model="fake",
                messages=[
                    {"role": "system", "content": info.instructions or ""},
                    {"role": "user", "content": str(user.content)},
                ],
                response_model=response_model,
                temperature=model_settings.get("temperature"),
                timeout=float(timeout) if isinstance(timeout, int | float) else None,
                max_tokens=model_settings.get("max_tokens"),
                extra_body=extra_body if isinstance(extra_body, dict) else None,
            )
        )
        response = self.completions.responses.get(output.name) or default_response(response_model)
        return ModelResponse(
            parts=[TextPart(content=self.completions.raw or response.model_dump_json())]
        )


@dataclass
class FakeJob:
    payload: bytes = b"{}"


@dataclass
class RecordingEnqueue:
    entrypoint: str
    payload: bytes
    dedupe_key: str | None = field(default=None)
