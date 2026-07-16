from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModelSettings

from ...config import settings
from ...ontology import Ontology
from ..base import HttpService, http_client, llm_model, request_throttle
from .models import GraphRequest, GraphResponse


class LLM:
    """Schema-constrained generation through the configured extraction model."""

    __slots__ = ("agent",)

    def __init__(self, model: Model) -> None:
        self.agent = Agent[None, str](model, deps_type=type(None))

    @classmethod
    def configured(cls) -> LLM:
        """Build the service from the live extraction settings."""
        return cls(llm_model(settings.llm_url, settings.llm_api_key, settings.llm_model))

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
            temperature=settings.llm_temperature if temperature is None else temperature,
            timeout=settings.llm_timeout if timeout is None else timeout,
            max_tokens=settings.llm_response_max_tokens if max_tokens is None else max_tokens,
        )
        if settings.llm_chat_template_kwargs:
            model_settings["extra_body"] = {
                "chat_template_kwargs": settings.llm_chat_template_kwargs
            }
        return (
            await self.agent.run(
                user,
                instructions=system,
                output_type=schema,
                model_settings=model_settings,
            )
        ).output


class GLiNER(HttpService):
    """Schema-constrained graph extraction through the GLiNER sidecar."""

    @classmethod
    def configured(cls) -> GLiNER:
        """Build the service from the shared GLiNER endpoint settings."""
        return cls(
            http_client(settings.gliner_url, "", settings.gliner_timeout),
            request_throttle(settings.gliner_url, settings.gliner_concurrency),
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
                threshold=settings.gliner_extract_threshold,
            ),
            GraphResponse,
        )
