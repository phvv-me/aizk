from patos import FrozenModel

from ...config import Settings, settings


class Provider(FrozenModel):
    """A named OpenAI-compatible chat endpoint, the url and default model switching is
    config."""

    name: str
    url: str
    model: str
    api_key: str = ""


# Explicit presets override only settings that still hold their defaults.
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
    """Return the registered provider of a name, or null when the name is not a known
    provider."""
    return PROVIDERS.get(name)


def at_default(field: str) -> bool:
    """Whether a settings field still holds its class default, so a preset may fill it."""
    return getattr(settings, field) == type(settings).model_fields[field].get_default(
        call_default_factory=True
    )


def provider_settings() -> Settings:
    """Overlay a named provider's endpoint onto the settings, leaving explicit overrides in
    place."""
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
