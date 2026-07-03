from patos import FrozenModel

from ...config import Settings, settings


class Provider(FrozenModel):
    """A named OpenAI-compatible chat endpoint, the url and default model switching is config.

    The registry keys these by name so pointing the extractor at a different LLM is naming the
    provider rather than editing three fields at once, Ollama the local-first default option and
    the hosted providers the cloud ones. An empty api_key rides the local servers that ignore it.

    name: registry key the provider is selected by, matched against `settings.llm_provider`.
    url: base URL of the provider's OpenAI-compatible chat endpoint.
    model: the chat model id the provider serves by default, filled in when none is set.
    api_key: bearer token for the endpoint, empty for a local server that ignores it.
    """

    name: str
    url: str
    model: str
    api_key: str = ""


# the named providers the extractor selects by `settings.llm_provider`, Ollama first-class and
# local-first, the hosted ones OpenAI-shaped so each is just a url and a served model. The default
# `vllm` label is deliberately absent so a stock settings load resolves unchanged and keeps its
# configured endpoint, and only an explicit provider name overlays a preset onto the fields left at
# their default.
PROVIDERS: dict[str, Provider] = {
    provider.name: provider
    for provider in (
        Provider(name="ollama", url="http://localhost:11434/v1", model="qwen2.5:7b"),
        Provider(name="cerebras", url="https://api.cerebras.ai/v1", model="llama-3.3-70b"),
        Provider(name="deepseek", url="https://api.deepseek.com/v1", model="deepseek-chat"),
        Provider(name="openai", url="https://api.openai.com/v1", model="gpt-4o-mini"),
    )
}


def resolve_provider(name: str) -> Provider | None:
    """Return the registered provider of a name, or null when the name is not a known provider.

    name: the provider key to look up, such as `ollama`.
    """
    return PROVIDERS.get(name)


def at_default(field: str) -> bool:
    """Whether a settings field still holds its class default, so a preset may fill it.

    An explicit environment or argument override always wins over a provider preset, so a field is
    only overlaid when it reads the value the Settings model itself defaults it to.

    field: the settings field name to test against its declared default.
    """
    return getattr(settings, field) == type(settings).model_fields[field].get_default(
        call_default_factory=True
    )


def provider_settings() -> Settings:
    """Overlay a named provider's endpoint onto the settings, leaving explicit overrides in place.

    When `settings.llm_provider` names a registered provider, its url, model, and key fill in only
    the fields still at their Settings default, so `AIZK_LLM_PROVIDER=ollama` switches the whole
    endpoint while an explicit `AIZK_LLM_URL` or `AIZK_LLM_MODEL` still wins. An unregistered
    provider label such as the default `vllm` returns the settings unchanged, so the stock load
    keeps the endpoint it was configured with.
    """
    provider = resolve_provider(settings.llm_provider)
    if provider is None:
        return settings
    overrides = {
        "llm_url": provider.url if at_default("llm_url") else settings.llm_url,
        "llm_model": provider.model if at_default("llm_model") else settings.llm_model,
        "llm_api_key": provider.api_key
        if provider.api_key and at_default("llm_api_key")
        else settings.llm_api_key,
    }
    return settings.model_copy(update=overrides)
