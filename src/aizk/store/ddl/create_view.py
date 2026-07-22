from collections.abc import Mapping

from sqlalchemy import Column, MetaData, Table
from sqlalchemy.schema import ExecutableDDLElement
from sqlalchemy.sql.selectable import FromClause, SelectBase


class CreateView(ExecutableDDLElement):
    """Create a mapped view from one typed select and optional PostgreSQL options."""

    inherit_cache = False

    def __init__(
        self,
        selectable: SelectBase,
        view_name: str,
        *,
        metadata: MetaData | None = None,
        postgresql_with: Mapping[str, str | bool | None] | None = None,
    ) -> None:
        self.selectable = selectable
        self.name = view_name
        self.postgresql_with = dict(postgresql_with or {})
        self.table = Table(
            view_name,
            metadata or MetaData(),
            *(Column(column.key, column.type) for column in selectable.selected_columns),
        )


class DropView(ExecutableDDLElement):
    """Drop one mapped view."""

    inherit_cache = False

    def __init__(self, table: FromClause, *, if_exists: bool = False) -> None:
        self.table = table
        self.if_exists = if_exists
