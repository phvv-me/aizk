from collections.abc import Callable
from typing import cast

from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import ExecutableDDLElement
from sqlalchemy.sql import ClauseElement
from sqlalchemy.sql.compiler import DDLCompiler

from .create_view import CreateView
from .extension import CreateExtension
from .grant import Grant


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


@compiles(CreateView, "postgresql")
def compile_create_view(
    element: CreateView,
    compiler: DDLCompiler,
    **kwargs: str | bool | None,
) -> str:
    """Insert PostgreSQL view options into SQLAlchemy 2.1's native rendering."""
    rendered = compiler.visit_create_view(element, **kwargs)
    if not element.postgresql_with:
        return rendered
    options = ", ".join(
        name
        if value is None
        else f"{name} = {str(value).lower() if isinstance(value, bool) else value}"
        for name, value in element.postgresql_with.items()
    )
    return rendered.replace(" AS ", f" WITH ({options}) AS ", 1)


def postgresql_sql(statement: ClauseElement | ExecutableDDLElement) -> str:
    """Compile typed SQLAlchemy SQL for an external PostgreSQL driver."""
    dialect = cast("Callable[[], Dialect]", postgresql.dialect)()
    return str(statement.compile(dialect=dialect)).strip()
