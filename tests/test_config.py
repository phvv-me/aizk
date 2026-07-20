import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from pydantic import ValidationError

from aizk.config import Settings, configure_logging

type PolicyValue = str | set[str] | dict[str, str] | dict[str, set[str]]

_COMPLETE_LOGTO = {
    "logto_url": "https://auth.test",
    "logto_client_id": "client",
    "logto_client_secret": "secret",
    "mcp_public_url": "https://aizk.test",
    "api_public_url": "https://api.aizk.test",
    "oauth_client_id": "oauth-client",
    "oauth_client_secret": "oauth-secret",
}
_LOGTO_DEPENDENCIES = tuple(name for name in _COMPLETE_LOGTO if name != "logto_url")
_COMPLETE_WEB = {
    **_COMPLETE_LOGTO,
    "web_public_url": "https://memory.test",
    "web_client_id": "web-client",
    "web_client_secret": "web-secret",
    "web_session_secret": "independent-session-secret-with-32-bytes",
}
_WEB_DEPENDENCIES = tuple(_COMPLETE_WEB)


def test_default_dsns_are_built_from_host_port_db_and_passwords() -> None:
    cfg = Settings(
        db_host="h", db_port=6000, db_name="mem", app_password="ap", admin_password="op"
    )
    assert cfg.database_url == "postgresql+asyncpg://aizk_app:ap@h:6000/mem"
    assert cfg.admin_database_url == "postgresql+asyncpg://aizk_admin:op@h:6000/mem"


@pytest.mark.parametrize("minimum_savings", [-0.01, 1.0])
def test_object_storage_rejects_a_nonreducing_compression_threshold(
    minimum_savings: float,
) -> None:
    with pytest.raises(ValidationError, match="compression_min_savings"):
        Settings(object_store_compression_min_savings=minimum_savings)


@pytest.mark.parametrize(
    ("field", "dsn"),
    [
        ("database_url", "postgresql+asyncpg://u:p@managed.example:5432/db?ssl=require"),
        ("admin_database_url", "postgresql+asyncpg://aizk:pw@managed:5432/db"),
    ],
)
def test_explicit_dsns_win_over_templates(field: str, dsn: str) -> None:
    cfg = Settings.model_validate({field: dsn, "db_host": "ignored"})
    assert getattr(cfg, field) == dsn


def test_asyncpg_dsns_drop_the_driver_tag() -> None:
    cfg = Settings(db_host="h", db_port=1, db_name="d", app_password="app")
    assert "+asyncpg" not in cfg.asyncpg_dsn
    assert "+asyncpg" not in cfg.admin_asyncpg_dsn
    assert cfg.asyncpg_dsn == "postgresql://aizk_app:app@h:1/d"


def test_app_role_reads_from_the_dsn_username() -> None:
    assert Settings().app_role == "aizk_app"
    assert Settings(database_url="postgresql+asyncpg://custom:p@h:1/d").app_role == "custom"


def test_chunk_denylist_parses_to_an_immutable_language_set() -> None:
    cfg = Settings(chunk_denylist="markdown,json,yaml")
    assert cfg.chunk_denylist_languages == frozenset({"markdown", "json", "yaml"})
    assert isinstance(cfg.chunk_denylist_languages, frozenset)


def test_llm_extra_body_parses_nested_json_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AIZK_LLM_EXTRA_BODY",
        '{"provider":{"zdr":true,"require_parameters":true},"reasoning":{"enabled":false}}',
    )

    config = Settings(_env_file=None)

    assert config.llm_extra_body == {
        "provider": {"zdr": True, "require_parameters": True},
        "reasoning": {"enabled": False},
    }


def test_llm_headers_parse_as_redacted_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AIZK_LLM_HEADERS",
        '{"Modal-Key":"wk-test","Modal-Secret":"ws-test"}',
    )

    config = Settings(_env_file=None)

    assert config.llm_headers["Modal-Key"].get_secret_value() == "wk-test"
    assert config.llm_headers["Modal-Secret"].get_secret_value() == "ws-test"
    assert config.model_dump(mode="json")["llm_headers"] == {
        "Modal-Key": "**********",
        "Modal-Secret": "**********",
    }


def test_complete_logto_configuration_derives_the_resource() -> None:
    assert Settings().mcp_resource_id == ""
    cfg = Settings(
        **_COMPLETE_LOGTO,
        logto_required_scopes={"control"},
        logto_write_permission="write:memory",
    )
    assert cfg.mcp_resource_id == "https://aizk.test/mcp"
    assert cfg.logto_required_scopes == frozenset({"control"})
    assert cfg.logto_write_permission == "write:memory"
    assert cfg.logto_role_permissions["admin"] == frozenset(
        {"write:memory", "manage:member", "delete:member"}
    )
    assert cfg.logto_retired_organization_permissions == frozenset({"invite:member"})
    assert cfg.logto_role_permissions["editor"] == frozenset({"write:memory"})
    assert set(cfg.logto_organization_roles) == {"admin", "editor", "viewer"}
    assert not {
        "oidc_issuer",
        "oidc_jwks_url",
        "oidc_algorithm",
        "oidc_audience",
        "mcp_http",
        "mcp_transport",
    } & set(type(cfg).model_fields)


def test_deprecated_settings_translate_into_the_live_policy() -> None:
    cfg = Settings(
        entity_resolution_threshold=0.8,
        logto_writable_roles={"admin"},
        logto_write_permission_description="Write shared things",
    )
    assert cfg.logto_organization_permissions["write:memory"] == "Write shared things"
    assert cfg.logto_role_permissions["admin"] == frozenset(
        {"write:memory", "manage:member", "delete:member"}
    )
    assert cfg.logto_role_permissions["editor"] == frozenset()  # write revoked by omission
    with pytest.raises(ValidationError, match="unknown roles"):
        Settings(logto_writable_roles={"ghost"})


def test_public_deployment_requires_an_https_api_origin() -> None:
    with pytest.raises(ValidationError, match="https api_public_url"):
        Settings.model_validate(_COMPLETE_LOGTO | {"api_public_url": "http://api.test"})


def test_complete_web_configuration_derives_the_exact_callback() -> None:
    cfg = Settings(**_COMPLETE_WEB)
    assert cfg.web_callback_url == "https://memory.test/auth/sign-in-callback"
    with pytest.raises(RuntimeError, match="web_public_url"):
        _ = Settings().web_callback_url


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ({"web_public_url": "http://memory.test"}, "HTTPS"),
        ({"web_session_secret": "too-short"}, "at least 32 bytes"),
        (
            {
                "web_session_secret": "independent-session-secret-with-32-bytes",
                "web_client_secret": "independent-session-secret-with-32-bytes",
            },
            "independent",
        ),
    ],
    ids=["public-http", "weak-secret", "reused-secret"],
)
def test_web_auth_rejects_unsafe_origins_and_session_secrets(
    configuration: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate(_COMPLETE_WEB | configuration)


@given(missing=st.sets(st.sampled_from(_WEB_DEPENDENCIES), min_size=1))
def test_web_auth_fails_closed_when_any_dependency_is_missing(missing: set[str]) -> None:
    configuration = _COMPLETE_WEB.copy()
    configuration.update(dict.fromkeys(missing, ""))
    with pytest.raises(ValidationError):
        Settings.model_validate(configuration)


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        (
            {"logto_managed_role_prefix": "aizk-", "logto_user_role": "member"},
            "managed_role_prefix",
        ),
        (
            {"logto_managed_role_prefix": ""},
            "at least 1 character",
        ),
        (
            {"logto_managed_role_prefix": "   "},
            "at least 1 character",
        ),
        (
            {"logto_user_role": " "},
            "at least 1 character",
        ),
        (
            {
                "logto_organization_roles": {
                    "admin": "Manage",
                    "editor": "Write",
                    "viewer": "Read",
                    "publisher": "Unmapped",
                }
            },
            "missing roles",
        ),
        (
            {"logto_required_scopes": {"missing"}, "logto_scope_descriptions": {}},
            "scope_descriptions",
        ),
        (
            {"logto_role_permissions": {"missing": set()}},
            "unknown roles",
        ),
        (
            {"logto_role_permissions": {"admin": {"missing"}}},
            "unknown permissions",
        ),
        (
            {"logto_write_permission": "missing"},
            "managed organization permission",
        ),
        (
            {"logto_creator_role": "missing"},
            "managed organization role",
        ),
        (
            {
                "logto_retired_organization_permissions": {"write:memory"},
            },
            "retired organization permissions",
        ),
    ],
    ids=[
        "unmanaged-user-role",
        "empty-prefix",
        "blank-prefix",
        "blank-user-role",
        "role-without-permissions",
        "missing-scope-description",
        "unknown-role",
        "unknown-permission",
        "unknown-write-permission",
        "unknown-creator-role",
        "active-retired-permission",
    ],
)
def test_logto_policy_rejects_unsafe_configuration(
    configuration: dict[str, PolicyValue], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate(configuration)


@given(missing=st.sets(st.sampled_from(_LOGTO_DEPENDENCIES), min_size=1))
def test_logto_auth_fails_closed_when_any_dependency_is_missing(missing: set[str]) -> None:
    configuration = _COMPLETE_LOGTO.copy()
    configuration.update(dict.fromkeys(missing, ""))

    with pytest.raises(ValidationError):
        Settings.model_validate(configuration)


@given(public_url=st.booleans(), require_auth=st.booleans())
def test_public_deployment_cannot_fall_back_to_auth_off(
    public_url: bool,
    require_auth: bool,
) -> None:
    assume(public_url or require_auth)
    configuration = {
        "mcp_public_url": "https://aizk.test" if public_url else None,
        "require_auth": require_auth,
    }
    with pytest.raises(ValidationError, match="requires logto_url"):
        Settings.model_validate(configuration)


def test_configure_logging_enables_and_disables_without_raising(settings: Settings) -> None:
    try:
        configure_logging("DEBUG")
        configure_logging("")
    finally:
        configure_logging(settings.log_level)


def test_extraction_backend_is_closed_to_supported_implementations() -> None:
    assert Settings(_env_file=None).extract_backend == "llm"
    with pytest.raises(ValidationError, match="gliner"):
        Settings.model_validate({"extract_backend": "unknown"})
