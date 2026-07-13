import pytest
from pydantic import ValidationError

from aizk.config import Settings, configure_logging


def test_default_dsns_are_built_from_host_port_db_and_passwords() -> None:
    cfg = Settings(
        db_host="h", db_port=6000, db_name="mem", app_password="ap", admin_password="op"
    )
    assert cfg.database_url == "postgresql+asyncpg://aizk_app:ap@h:6000/mem"
    assert cfg.admin_database_url == "postgresql+asyncpg://aizk_admin:op@h:6000/mem"


def test_explicit_dsn_wins_over_the_template() -> None:
    explicit = "postgresql+asyncpg://u:p@managed.example:5432/db?ssl=require"
    cfg = Settings(database_url=explicit, db_host="ignored")
    assert cfg.database_url == explicit
    assert cfg.admin_database_url.startswith("postgresql+asyncpg://aizk_admin:")


def test_explicit_admin_dsn_is_also_preserved() -> None:
    admin = "postgresql+asyncpg://aizk:pw@managed:5432/db"
    cfg = Settings(admin_database_url=admin, db_host="ignored")
    assert cfg.admin_database_url == admin


def test_asyncpg_dsns_drop_the_driver_tag() -> None:
    cfg = Settings(db_host="h", db_port=1, db_name="d")
    assert "+asyncpg" not in cfg.asyncpg_dsn
    assert "+asyncpg" not in cfg.admin_asyncpg_dsn
    assert cfg.asyncpg_dsn == "postgresql://aizk_app:aizk_app@h:1/d"


def test_app_role_reads_from_the_dsn_username() -> None:
    assert Settings().app_role == "aizk_app"
    assert Settings(database_url="postgresql+asyncpg://custom:p@h:1/d").app_role == "custom"


def test_chunk_denylist_parses_to_an_immutable_language_set() -> None:
    cfg = Settings(chunk_denylist="markdown,json,yaml")
    assert cfg.chunk_denylist_languages == frozenset({"markdown", "json", "yaml"})
    assert isinstance(cfg.chunk_denylist_languages, frozenset)


def test_logto_configuration_derives_the_resource_and_rejects_partial_auth() -> None:
    assert Settings().mcp_resource_id == ""
    with pytest.raises(ValidationError, match="mcp_public_url"):
        Settings(logto_url="https://auth.test")

    cfg = Settings(
        logto_url="https://auth.test",
        logto_client_id="client",
        logto_client_secret="secret",
        mcp_public_url="https://aizk.test",
        logto_required_scopes={"control"},
        logto_writable_roles={"admin", "editor"},
    )
    assert cfg.mcp_resource_id == "https://aizk.test/mcp"
    assert cfg.logto_required_scopes == frozenset({"control"})
    assert not {
        "oidc_issuer",
        "oidc_jwks_url",
        "oidc_algorithm",
        "oidc_audience",
        "mcp_http",
        "mcp_transport",
    } & set(type(cfg).model_fields)


def test_configure_logging_enables_and_disables_without_raising(settings: Settings) -> None:
    try:
        configure_logging("DEBUG")
        configure_logging("")
    finally:
        configure_logging(settings.log_level)
