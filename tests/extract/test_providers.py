import pytest

from aizk.config import Settings
from aizk.extract.llm.providers import (
    PROVIDERS,
    Provider,
    at_default,
    provider_settings,
    resolve_provider,
)


@pytest.mark.parametrize("name", list(PROVIDERS))
def test_registered_provider_resolves_to_its_preset(name: str) -> None:
    provider = resolve_provider(name)
    assert isinstance(provider, Provider)
    assert provider.name == name and provider.url and provider.model


def test_unregistered_label_resolves_to_none() -> None:
    assert resolve_provider("vllm") is None
    assert resolve_provider("not-a-provider") is None


def _class_default(field: str) -> object:
    return Settings.model_fields[field].get_default(call_default_factory=True)


def test_at_default_reads_the_field_class_default(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_url", _class_default("llm_url"))
    assert at_default("llm_url")
    monkeypatch.setattr(settings, "llm_url", "http://elsewhere/v1")
    assert not at_default("llm_url")


def test_unregistered_provider_returns_settings_unchanged(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "vllm")
    assert provider_settings() is settings


def test_named_provider_overlays_only_default_fields(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "cerebras")
    monkeypatch.setattr(settings, "llm_url", _class_default("llm_url"))
    monkeypatch.setattr(settings, "llm_model", _class_default("llm_model"))
    overlaid = provider_settings()
    assert overlaid.llm_url == PROVIDERS["cerebras"].url
    assert overlaid.llm_model == PROVIDERS["cerebras"].model

    monkeypatch.setattr(settings, "llm_url", "http://explicit/v1")
    kept = provider_settings()
    assert kept.llm_url == "http://explicit/v1"
