import asyncio
import json
import uuid

import pytest
import respx

from aizk.config import Settings, configure_logging
from aizk.config import settings as global_settings
from aizk.serving.embed import Embedder

EMBED_URL = "http://embed.test/v1"
EMBEDDINGS = f"{EMBED_URL}/embeddings"


def test_llm_api_key_defaults_to_the_ambient_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """With AIZK_LLM_API_KEY unset the LLM key falls back to the existing OPENAI_API_KEY."""
    monkeypatch.delenv("AIZK_LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient")

    assert Settings().llm_api_key == "sk-ambient"


def test_aizk_llm_api_key_overrides_the_ambient_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit AIZK_LLM_API_KEY takes precedence over the ambient OPENAI_API_KEY."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient")
    monkeypatch.setenv("AIZK_LLM_API_KEY", "sk-explicit")

    assert Settings().llm_api_key == "sk-explicit"


def test_llm_api_key_is_empty_with_no_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """With neither key in the environment the LLM key is empty, the local-server default."""
    monkeypatch.delenv("AIZK_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert Settings().llm_api_key == ""


def test_cerebras_is_selected_by_pointing_the_url_and_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting Cerebras is just setting the provider label, url, and key, all OpenAI shaped."""
    monkeypatch.delenv("AIZK_LLM_API_KEY", raising=False)
    settings = Settings(
        llm_provider="cerebras",
        llm_url="https://api.cerebras.ai/v1",
        llm_model="llama-3.3-70b",
        llm_api_key="csk-secret",
    )

    assert settings.llm_provider == "cerebras"
    assert settings.llm_url == "https://api.cerebras.ai/v1"
    assert settings.llm_api_key == "csk-secret"


def test_serving_knobs_default_to_the_co_resident_vllm_containers() -> None:
    """embed, rerank, and llm each default to their own co-resident vLLM container's port."""
    settings = Settings()

    assert settings.embed_url == "http://localhost:8000/v1"
    assert settings.embed_model == "qwen3-vl-emb"
    assert settings.rerank_url == "http://localhost:8001/v1"
    assert settings.rerank_model == "Qwen/Qwen3-Reranker-4B"
    assert settings.llm_url == "http://localhost:8002/v1"
    assert settings.llm_model == "qwen3-llm"


def test_embed_url_resolves_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pointing at a different embed endpoint is an env edit, AIZK_EMBED_URL read directly."""
    monkeypatch.setenv("AIZK_EMBED_URL", "http://gpu-box:8000/v1")

    assert Settings().embed_url == "http://gpu-box:8000/v1"


@respx.mock
def test_embedder_returns_a_1024_d_vector_at_the_default_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The embedder requests dimensions=1024 and validates every returned row against it."""
    dim = Settings().embed_dim
    assert dim == 1024
    row = [0.0] * dim
    route = respx.post(EMBEDDINGS).respond(
        json={
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": row}],
            "model": "qwen3-vl-emb",
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }
    )
    monkeypatch.setattr(global_settings, "embed_url", EMBED_URL)
    monkeypatch.setattr(global_settings, "embed_model", "qwen3-vl-emb")
    monkeypatch.setattr(global_settings, "embed_dim", dim)
    if "singleton_instance" in Embedder.__dict__:
        delattr(Embedder, "singleton_instance")
    [vector] = asyncio.run(Embedder().embed(["a chunk"]))
    if "singleton_instance" in Embedder.__dict__:
        delattr(Embedder, "singleton_instance")

    assert len(vector) == 1024
    assert json.loads(route.calls.last.request.content)["dimensions"] == 1024


def test_asyncpg_dsn_drops_the_driver_tag_from_database_url() -> None:
    """`asyncpg_dsn` strips the `+asyncpg` tag so `asyncpg.connect` can dial it directly."""
    settings = Settings(database_url="postgresql+asyncpg://writer@host:5432/db")

    assert settings.asyncpg_dsn == "postgresql://writer@host:5432/db"


def test_admin_asyncpg_dsn_drops_the_driver_tag_from_admin_database_url() -> None:
    """`admin_asyncpg_dsn` mirrors `asyncpg_dsn` for the owning role's DSN."""
    settings = Settings(admin_database_url="postgresql+asyncpg://owner@host:5432/db")

    assert settings.admin_asyncpg_dsn == "postgresql://owner@host:5432/db"


def test_app_role_reads_the_username_or_falls_back_to_aizk_app() -> None:
    """`app_role` is the app DSN's username, defaulting to aizk_app when the DSN omits one."""
    assert Settings(database_url="postgresql+asyncpg://writer@host:5432/db").app_role == "writer"
    assert Settings(database_url="postgresql+asyncpg://host:5432/db").app_role == "aizk_app"


def test_config_derived_fields_carry_their_moved_defaults() -> None:
    """Every constant moved into Settings keeps the value the module constant used to hold."""
    settings = Settings()

    assert settings.ppr_margin == 0.35
    assert settings.gap_seed_terms == 2
    assert settings.decay_floor == 0.25
    assert settings.similar_facts == 5
    assert settings.rrf_k == 60
    assert settings.fusion_depth == 50
    assert settings.louvain_seed == 7
    assert settings.anonymous_principal_id == uuid.UUID(int=0)
    assert settings.system_principal_id == uuid.UUID("00000000-0000-0000-0000-000000000001")


def test_log_level_defaults_to_info_and_resolves_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """log_level defaults to INFO and reads back an env override, empty disabling the logger."""
    monkeypatch.delenv("AIZK_LOG_LEVEL", raising=False)
    assert Settings().log_level == "INFO"

    monkeypatch.setenv("AIZK_LOG_LEVEL", "DEBUG")
    assert Settings().log_level == "DEBUG"

    monkeypatch.setenv("AIZK_LOG_LEVEL", "")
    assert Settings().log_level == ""


@pytest.mark.parametrize("level", ["INFO", ""], ids=["enabled", "silenced"])
def test_configure_logging_enables_a_level_or_silences_the_library(level: str) -> None:
    """A level wires a stderr sink, an empty level disables aizk's logger, both restored after.

    Drives both import-time branches of the one logging setup, then re-applies the ambient level so
    the shared loguru logger is left exactly as the package configured it at import.
    """
    try:
        configure_logging(level)
    finally:
        configure_logging(global_settings.log_level)
