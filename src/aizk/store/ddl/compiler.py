from collections.abc import Callable
from typing import cast

from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import ExecutableDDLElement
from sqlalchemy.sql import ClauseElement
from sqlalchemy.sql.compiler import DDLCompiler

from .create_view import CreateView, DropView
from .extension import CreateExtension
from .grant import Grant


@compiles(Grant, "cockroachdb")
@compiles(Grant, "postgresql")
def compile_grant(
    element: Grant,
    compiler: DDLCompiler,
    **kwargs: str | bool | None,
) -> str:
    """Compile a grant from its target's SQL template."""
    del kwargs
    quote = compiler.preparer.quote
    return element.grant_target.value.format(
        privileges=", ".join(element.privileges),
        name=quote(element.name),
        role=quote(element.role),
    )


@compiles(CreateExtension, "postgresql")
def compile_create_extension(
    element: CreateExtension,
    compiler: DDLCompiler,
    **kwargs: str | bool | None,
) -> str:
    """Compile idempotent extension creation."""
    del kwargs
    return f"CREATE EXTENSION IF NOT EXISTS {compiler.preparer.quote(element.name)}"


@compiles(CreateView)
def compile_create_view(
    element: CreateView,
    compiler: DDLCompiler,
    **kwargs: str | bool | None,
) -> str:
    """Compile a mapped view and its PostgreSQL-compatible options."""
    del kwargs
    options = ""
    if element.postgresql_with:
        values = ", ".join(
            name
            if value is None
            else f"{name} = {str(value).lower() if isinstance(value, bool) else value}"
            for name, value in element.postgresql_with.items()
        )
        options = f" WITH ({values})"
    selectable = compiler.sql_compiler.process(element.selectable, literal_binds=True)
    return f"CREATE VIEW {compiler.preparer.quote(element.name)}{options} AS {selectable}"


@compiles(DropView)
def compile_drop_view(
    element: DropView,
    compiler: DDLCompiler,
    **kwargs: str | bool | None,
) -> str:
    """Compile a mapped view drop."""
    del kwargs
    exists = " IF EXISTS" if element.if_exists else ""
    return f"DROP VIEW{exists} {compiler.preparer.format_table(element.table)}"


def postgresql_sql(statement: ClauseElement | ExecutableDDLElement) -> str:
    """Compile typed SQLAlchemy SQL for an external PostgreSQL driver."""
    dialect = cast("Callable[[], Dialect]", postgresql.dialect)()
    return str(statement.compile(dialect=dialect)).strip()
