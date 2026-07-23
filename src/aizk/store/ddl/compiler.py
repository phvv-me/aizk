from collections.abc import Callable
from typing import cast

from rls.ddl import RLSStatement
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


@compiles(RLSStatement, "cockroachdb")
def compile_cockroach_rls(
    element: RLSStatement,
    compiler: DDLCompiler,
    **kwargs: bool,
) -> str:
    """Compile PostgreSQL-compatible row security DDL for CockroachDB."""
    del kwargs
    quote = compiler.preparer.quote
    policy = element.policy
    raw_name = policy.name if policy is not None else element.name
    return element.action.value.format(
        table=compiler.preparer.format_table(element.table),
        name=quote(raw_name) if raw_name is not None else "",
        mode="PERMISSIVE" if policy is not None and policy.permissive else "RESTRICTIVE",
        command=policy.command.sql if policy is not None else "",
        roles=", ".join(quote(role) for role in policy.roles) if policy is not None else "",
        using=f" USING ({policy.using})" if policy is not None and policy.using else "",
        check=f" WITH CHECK ({policy.check})" if policy is not None and policy.check else "",
    )


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
