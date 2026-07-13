from typing import ClassVar, cast

import inflection
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql.base import RESERVED_WORDS
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import declared_attr, registry
from sqlmodel import SQLModel

type Json = bool | int | float | str | None | list["Json"] | dict[str, "Json"]

_REGISTRY = registry()


class MappedBase(SQLModel, registry=_REGISTRY):
    """Shared SQLModel mapping and portable record serialization."""

    model_config = {"ignored_types": (hybrid_property,)}
    mapper_registry: ClassVar[registry] = _REGISTRY
    record_excluded: ClassVar[frozenset[str]] = frozenset({"embedding"})
    __table__: ClassVar[Table]
    # sqlmodel redefines its ClassVar `__tablename__` as a declared_attr under its own
    # `type: ignore`, so any redeclaration here trips the same third-party stub gap.
    __tablename__: ClassVar[str]  # pyrefly: ignore[bad-override]

    def record(self) -> dict[str, Json]:
        """Serialize a mapped row with its table identity."""
        return {"table": self.__tablename__} | self.model_dump(
            mode="json",
            exclude=set(self.record_excluded),
        )


class TableBase(MappedBase):
    """Declarative table base with singular snake case names."""

    @declared_attr.directive
    def __tablename__(cls) -> str:
        name = inflection.underscore(cls.__name__)
        return f"{name}_" if name in RESERVED_WORDS else name

    __tablename__ = cast(str, __tablename__)
