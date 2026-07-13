import inflection
import rls
from sqlalchemy import Column, Table
from sqlalchemy.dialects.postgresql.base import RESERVED_WORDS
from sqlalchemy.sql import Select

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
    def __view_select__(cls) -> Select:
        """Return the select defining a concrete view."""
        raise NotImplementedError

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: bool) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        if "__view_select__" not in cls.__dict__:
            return
        name = inflection.underscore(cls.__name__)
        name = f"{name}_" if name in RESERVED_WORDS else name
        selection = cls.__view_select__()
        table = Table(
            name,
            MappedBase.metadata,
            *(
                Column(column.name, column.type, primary_key=(index == 0))
                for index, column in enumerate(selection.selected_columns)
            ),
            info={"is_view": True},
        )
        MappedBase.metadata.info.setdefault("views", set()).add(name)
        cls.__tablename__ = name
        MappedBase.mapper_registry.map_imperatively(cls, table)
