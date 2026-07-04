from aizk.config import Settings, configure_logging


def test_default_dsns_are_built_from_host_port_db_and_passwords() -> None:
    """An unset DSN is templated from the host/port/db and role passwords, both roles distinct."""
    cfg = Settings(
        db_host="h", db_port=6000, db_name="mem", app_password="ap", admin_password="op"
    )
    assert cfg.database_url == "postgresql+asyncpg://aizk_app:ap@h:6000/mem"
    assert cfg.admin_database_url == "postgresql+asyncpg://aizk:op@h:6000/mem"


def test_explicit_dsn_wins_over_the_template() -> None:
    """A supplied DSN is never overwritten by the host/port/db template, the cloud-profile door."""
    explicit = "postgresql+asyncpg://u:p@managed.example:5432/db?ssl=require"
    cfg = Settings(database_url=explicit, db_host="ignored")
    assert cfg.database_url == explicit
    # the admin DSN, left unset, still fills from the template independently
    assert cfg.admin_database_url.startswith("postgresql+asyncpg://aizk:")


def test_explicit_admin_dsn_is_also_preserved() -> None:
    """A supplied admin DSN is likewise never overwritten by the template."""
    admin = "postgresql+asyncpg://aizk:pw@managed:5432/db"
    cfg = Settings(admin_database_url=admin, db_host="ignored")
    assert cfg.admin_database_url == admin


def test_asyncpg_dsns_drop_the_driver_tag() -> None:
    """The asyncpg-facing DSNs strip `+asyncpg`, the form pgqueuer's driver dials directly."""
    cfg = Settings(db_host="h", db_port=1, db_name="d")
    assert "+asyncpg" not in cfg.asyncpg_dsn
    assert "+asyncpg" not in cfg.admin_asyncpg_dsn
    assert cfg.asyncpg_dsn == "postgresql://aizk_app:aizk_app@h:1/d"


def test_app_role_reads_from_the_dsn_username() -> None:
    """`app_role` reflects the DSN's own username rather than a hardcoded constant."""
    assert Settings().app_role == "aizk_app"
    assert Settings(database_url="postgresql+asyncpg://custom:p@h:1/d").app_role == "custom"


def test_chunk_denylist_parses_to_an_immutable_language_set() -> None:
    """The comma-separated denylist parses to a frozenset, each named language a member."""
    cfg = Settings(chunk_denylist="markdown,json,yaml")
    assert cfg.chunk_denylist_languages == frozenset({"markdown", "json", "yaml"})
    assert isinstance(cfg.chunk_denylist_languages, frozenset)


def test_configure_logging_enables_and_disables_without_raising(settings: Settings) -> None:
    """A non-empty level enables the stderr sink; an empty level disables the logger entirely."""
    try:
        configure_logging("DEBUG")
        configure_logging("")
    finally:
        configure_logging(settings.log_level)
