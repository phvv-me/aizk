from enum import StrEnum


class GrantTarget(StrEnum):
    """Quoted PostgreSQL grant templates."""

    schema = "GRANT {privileges} ON SCHEMA {name} TO {role}"
    all_tables = "GRANT {privileges} ON ALL TABLES IN SCHEMA {name} TO {role}"
    all_sequences = "GRANT {privileges} ON ALL SEQUENCES IN SCHEMA {name} TO {role}"
    table = "GRANT {privileges} ON {name} TO {role}"
    sequence = "GRANT {privileges} ON SEQUENCE {name} TO {role}"
    default_tables = (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA {name} GRANT {privileges} ON TABLES TO {role}"
    )
    default_sequences = (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA {name} GRANT {privileges} ON SEQUENCES TO {role}"
    )
