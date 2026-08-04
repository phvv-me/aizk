from typing import ClassVar

from patos import sql
from sqlalchemy import Table
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import registry

# SQLModel does not publicly export its config TypedDict even though table models require it.
from sqlmodel.main import SQLModelConfig

type Json = bool | int | float | str | None | list["Json"] | dict[str, "Json"]

_REGISTRY = registry()


class MappedBase(sql.Model, registry=_REGISTRY):
    """Shared SQLModel mapping and portable record serialization."""

    model_config = SQLModelConfig(ignored_types=(hybrid_property,))
    mapper_registry: ClassVar[registry] = _REGISTRY
    record_excluded: ClassVar[frozenset[str]] = frozenset({"embedding"})
    __table__: ClassVar[Table]

    def record(self) -> dict[str, Json]:
        """Serialize a mapped row with its table identity."""
        return {"table": self.__tablename__} | self.model_dump(
            mode="json",
            exclude=set(self.record_excluded),
        )


class TableBase(MappedBase):
    """Declarative table base with singular snake case names."""
