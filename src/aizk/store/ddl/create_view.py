from collections.abc import Mapping
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.sql import Select
from sqlalchemy.sql.ddl import CreateView as SQLAlchemyCreateView


# FIXME: Delete this shim after a SQLAlchemy release includes issue 13432.
class CreateView(SQLAlchemyCreateView):
    """Backport PostgreSQL options onto SQLAlchemy 2.1's native view object.

    SQLAlchemy main supports this after issue 13432, but 2.1.0b3 predates that merge.
    The subclass keeps the upstream constructor and table mapping while adding only the
    missing option payload. Delete it when the next 2.1 release includes the upstream fix.
    """

    def __init__(
        self,
        selectable: Select[Any],
        view_name: str,
        *,
        metadata: MetaData | None = None,
        postgresql_with: Mapping[str, str | bool | None] | None = None,
    ) -> None:
        self.postgresql_with = dict(postgresql_with or {})
        super().__init__(selectable, view_name, metadata=metadata)
