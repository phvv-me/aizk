from typing import Any

import rls
from sqlalchemy import PrimaryKeyConstraint
from sqlalchemy.sql import Select

from ..ddl import CreateView
from .base import MappedBase


class ViewBase(MappedBase):
    """Read-only SQLModel base mapped from one defining `SELECT`.

    Views carry no policies of their own. Every view is created `security_invoker`, so the
    underlying tables' forced row security governs what a caller can read through it. No
    view is a security barrier: row security already fences every base-table scan, the view
    qualifiers only hide temporal states of rows the caller may read, and a barrier would
    block the planner from pushing vector-distance ordering into the content indexes.
    """

    __rls__ = rls.Open()

    @classmethod
    def __view_select__(cls) -> Select[Any]:
        """Return the select defining a concrete view."""
        raise NotImplementedError

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: bool) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        if "__view_select__" not in cls.__dict__:
            return
        name = cls.table_name()
        view = CreateView(
            cls.__view_select__(),
            name,
            metadata=MappedBase.metadata,
            postgresql_with={"security_invoker": True},
        )
        table = view.table
        table.info["is_view"] = True
        table.append_constraint(PrimaryKeyConstraint(next(iter(table.c))))
        MappedBase.metadata.info.setdefault("views", set()).add(name)
        type.__setattr__(cls, "__tablename__", name)
        MappedBase.mapper_registry.map_imperatively(cls, table)
