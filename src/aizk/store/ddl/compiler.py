from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import ExecutableDDLElement
from sqlalchemy.sql import ClauseElement
from sqlalchemy.sql.compiler import DDLCompiler

from .create_view import CreateView
from .drop_view import DropView
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
    """Compile a typed select into a security-invoker view.

    No security_barrier: row security on the underlying tables is enforced at their scans
    regardless, a view's own qualifiers only hide temporal states of rows the caller may
    already read, and a barrier would fence the planner away from the vector indexes.
    """
    del kwargs
    name = compiler.preparer.quote(element.name)
    select = element.select.compile(
        dialect=compiler.dialect,
        compile_kwargs={"literal_binds": True},
    )
    options = "security_invoker = true"
    return f"CREATE VIEW {name} WITH ({options}) AS\n{select}"


@compiles(DropView, "postgresql")
def compile_drop_view(
    element: DropView,
    compiler: DDLCompiler,
    **kwargs: str | bool | None,
) -> str:
    """Compile an idempotent view drop."""
    del kwargs
    return f"DROP VIEW IF EXISTS {compiler.preparer.quote(element.name)}"


def postgresql_sql(statement: ClauseElement | ExecutableDDLElement) -> str:
    """Compile typed SQLAlchemy SQL for an external PostgreSQL driver."""
    return str(statement.compile(dialect=postgresql.dialect()))
