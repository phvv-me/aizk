import pytest

from aizk.config import settings
from aizk.extract.llm import PROVIDERS, Provider, provider_settings, resolve_provider


def test_ollama_resolves_as_a_first_class_local_provider() -> None:
    """The Ollama entry resolves from the registry with its local endpoint and a served model."""
    provider = resolve_provider("ollama")

    assert isinstance(provider, Provider)
    assert provider.name == "ollama"
    assert provider.url == "http://localhost:11434/v1"
    assert provider.model


def test_an_unregistered_provider_name_resolves_to_null() -> None:
    """A label the registry does not carry resolves to null, the signal to leave settings alone."""
    assert resolve_provider("vllm") is None
    assert "vllm" not in PROVIDERS


def test_provider_settings_overlays_ollama_onto_the_default_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting Ollama switches the url and model where they sit at their Settings default."""
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "llm_url", "http://localhost:11434/v1")
    resolved = provider_settings()

    assert resolved.llm_url == "http://localhost:11434/v1"
    assert resolved.llm_model == PROVIDERS["ollama"].model


def test_an_explicit_url_and_model_win_over_the_provider_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit endpoint overrides the preset, so the named provider only fills the defaults."""
    monkeypatch.setattr(settings, "llm_provider", "cerebras")
    monkeypatch.setattr(settings, "llm_url", "https://custom.example/v1")
    monkeypatch.setattr(settings, "llm_model", "my-model")
    resolved = provider_settings()

    assert resolved.llm_url == "https://custom.example/v1"
    assert resolved.llm_model == "my-model"


def test_the_default_vllm_label_leaves_the_settings_unchanged() -> None:
    """The stock vllm label is not a registered provider, so the endpoint stays as configured."""
    resolved = provider_settings()

    assert resolved.llm_url == settings.llm_url
    assert resolved.llm_model == settings.llm_model
