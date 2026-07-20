from functools import cached_property
from typing import Protocol, runtime_checkable

from patos import FrozenFlexModel
from pydantic import BaseModel, JsonValue
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModelSettings

from ...config import Settings
from ...ontology import Ontology
from ..base import HttpService, http_client, llm_model, request_throttle
from .models import GraphRequest, GraphResponse


class LLM(FrozenFlexModel):
    """Schema-constrained generation through the configured extraction model."""

    model: Model
    temperature: float = 0.0
    timeout: float = 300.0
    response_max_tokens: int = 512
    chat_template_kwargs: dict[str, bool] = {}
    extra_body: dict[str, JsonValue] = {}

    @cached_property
    def agent(self) -> Agent[None, str]:
        """The typed agent bound to this endpoint's model."""
        return Agent[None, str](self.model, deps_type=type(None))

    @classmethod
    def from_settings(cls, config: Settings) -> LLM:
        """Build the service from explicit extraction settings."""
        return cls(
            model=llm_model(
                config.llm_url,
                config.llm_api_key,
                config.llm_model,
                config.llm_timeout,
                tuple(
                    sorted(
                        (name, value.get_secret_value())
                        for name, value in config.llm_headers.items()
                    )
                ),
            ),
            temperature=config.llm_temperature,
            timeout=config.llm_timeout,
            response_max_tokens=config.llm_response_max_tokens,
            chat_template_kwargs=config.llm_chat_template_kwargs,
            extra_body=config.llm_extra_body,
        )

    async def generate[ResponseT: BaseModel](
        self,
        system: str,
        user: str,
        schema: type[ResponseT],
        *,
        temperature: float | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> ResponseT:
        """Run one typed model turn and return its validated response."""
        model_settings = OpenAIChatModelSettings(
            temperature=self.temperature if temperature is None else temperature,
            timeout=self.timeout if timeout is None else timeout,
            max_tokens=self.response_max_tokens if max_tokens is None else max_tokens,
        )
        extra_body = dict(self.extra_body)
        if self.chat_template_kwargs:
            configured_template = extra_body.get("chat_template_kwargs")
            extra_body["chat_template_kwargs"] = {
                **(configured_template if isinstance(configured_template, dict) else {}),
                **self.chat_template_kwargs,
            }
        if extra_body:
            model_settings["extra_body"] = extra_body
        return (
            await self.agent.run(
                user,
                instructions=system,
                output_type=schema,
                model_settings=model_settings,
            )
        ).output


@runtime_checkable
class GraphBackend(Protocol):
    """The grounded graph extraction surface the GLiNER extractor consumes."""

    async def extract(self, text: str) -> GraphResponse: ...


class GLiNER(HttpService):
    """Schema-constrained graph extraction through the GLiNER sidecar."""

    extract_threshold: float

    @classmethod
    def from_settings(cls, config: Settings) -> GLiNER:
        """Build the service from explicit GLiNER endpoint settings."""
        return cls(
            client=http_client(config.gliner_url, "", config.gliner_timeout),
            throttle=request_throttle(config.gliner_url, config.gliner_concurrency),
            extract_threshold=config.gliner_extract_threshold,
        )

    async def extract(self, text: str) -> GraphResponse:
        """Extract ontology entities and relations from one text in a single pass."""
        ontology = Ontology.current()
        return await self.post(
            "/graph",
            GraphRequest(
                text=text,
                entity_types=ontology.entity_descriptions,
                relation_types=ontology.relation_descriptions,
                threshold=self.extract_threshold,
            ),
            GraphResponse,
        )
