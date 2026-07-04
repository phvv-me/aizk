from sqlalchemy import Column, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from .base import MappedBase, aizk_registry, derive_tablename

# a small, aizk-agnostic extension letting SQLModel map a read-only database VIEW the same way it
# maps a table: a subclass declares its columns as plain typed fields exactly like a `TableBase`
# table plus one classmethod, `__view_select__`, returning the `Select` the view is defined by.
# That `Select` is the view's single source of truth. `register_view` reads its own
# `selected_columns` back to build the mapped `Table` (so a view's column list is written once
# rather than kept in sync by hand against a separate column list), and `create_view_ddl` compiles
# the identical `Select` into the `CREATE VIEW` a migration executes, so the mapped class and the
# DDL can never drift apart. Nothing here imports an aizk model; a concrete view module supplies
# its own `__view_select__` over whatever tables it joins.


class ViewBase(MappedBase):
    """Declarative base for a read-only database VIEW, mapped like `TableBase` but backed by a
    `SELECT` instead of a `CREATE TABLE`.

    A concrete subclass declares its own `__view_select__` and is mapped automatically the moment
    its class statement finishes, mirroring how a `TableBase` subclass is mapped by `table=True`.
    The hook is `__pydantic_init_subclass__`, not `__init_subclass__`: SQLModel's pydantic field
    collection deletes a fresh subclass's raw annotated attributes immediately after
    `__init_subclass__` fires, and that delete raises once instrumented attributes already sit
    over the same names, so mapping must wait until pydantic is done with the class, which is
    exactly when `__pydantic_init_subclass__` runs. A subclass that leaves `__view_select__`
    undeclared (an intermediate abstract base) stays unmapped, the same way `ViewBase` itself
    does.
    """

    @classmethod
    def __view_select__(cls) -> Select:
        """The `SELECT` this view is defined by; every concrete subclass overrides this."""
        raise NotImplementedError

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: bool) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        if "__view_select__" in cls.__dict__:
            register_view(cls)


def register_view(view: type[ViewBase]) -> None:
    """Map `view` onto its own `__view_select__`, turning a `ViewBase` subclass into a mapped,
    queryable, read-only ORM class.

    Called by `ViewBase.__pydantic_init_subclass__` for every subclass declaring its own
    `__view_select__`, never by hand. The first selected column is always the view's primary key,
    the `id` every view in this codebase leads with; `info={"is_view": True}` and the `views`
    name set on the shared metadata are what `store.migrations.env`'s autogenerate
    `include_object` filter and `create_view_ddl`'s own callers key off to leave a view out of
    the table-diff surface.

    view: the `ViewBase` subclass to map.
    """
    name = derive_tablename(view.__name__)
    select = view.__view_select__()
    table = Table(
        name,
        MappedBase.metadata,
        *(
            Column(column.name, column.type, primary_key=(index == 0))
            for index, column in enumerate(select.selected_columns)
        ),
        info={"is_view": True},
    )
    MappedBase.metadata.info.setdefault("views", set()).add(name)
    view.__tablename__ = name
    aizk_registry.map_imperatively(view, table)


def create_view_ddl(name: str, select: Select) -> str:
    """The `CREATE VIEW ... WITH (security_invoker = true)` DDL for one view's `SELECT`.

    `security_invoker = true` is load-bearing: a default (security_definer-like) view runs as the
    view's owning role rather than the querying session and silently bypasses row level security,
    so every view over an RLS-protected table needs it. Compiled with literal binds so the DDL
    carries no bound parameters, which a `CREATE VIEW` statement cannot take.

    name: view name, `derive_tablename`'s output for the `ViewBase` subclass.
    select: the `SELECT` the view is defined by, `cls.__view_select__()`'s return value.
    """
    compiled = select.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    return f"CREATE VIEW {name} WITH (security_invoker = true) AS\n{compiled}"


def drop_view_ddl(name: str) -> str:
    """The `DROP VIEW IF EXISTS` DDL reversing `create_view_ddl`.

    name: view name to drop.
    """
    return f"DROP VIEW IF EXISTS {name}"
