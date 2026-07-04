from typing import ClassVar, cast

import inflection
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql.base import RESERVED_WORDS
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import declared_attr, registry
from sqlmodel import SQLModel

# the json-ready value shapes a serialized row carries, uuid and datetime already stringified
type Json = bool | int | float | str | None | list["Json"] | dict[str, "Json"]

# aizk's own registry/metadata rather than SQLModel's process-global default, so importing a model
# module here never leaks a table onto whatever else in the process happens to import sqlmodel.
aizk_registry = registry()


def derive_tablename(name: str) -> str:
    """Singular snake_case table name derived from a class name, `_`-suffixed on a reserved word.

    `SessionItem` derives to `session_item`. `Group` collides with GROUP, a reserved word in
    `RESERVED_WORDS` (the postgresql dialect's own catalog, the dialect every DSN in this codebase
    dials), so it derives to `group_` instead, avoiding the manual quoting every raw `text()`
    statement touching an unsuffixed `group` would otherwise need. Shared by `TableBase`'s own
    `__tablename__` and `ViewBase`'s, so a table and a view can never derive the reserved-word
    suffix two different ways.

    name: the class name to derive a table name from.
    """
    derived = inflection.underscore(name)
    return f"{derived}_" if derived in RESERVED_WORDS else derived


class MappedBase(SQLModel, registry=aizk_registry):
    """Serialization and typed-table surface shared by every mapped class, table or view alike.

    A SQLModel base rather than a plain SQLAlchemy `DeclarativeBase`, so every concrete model is
    simultaneously the mapped ORM class and its own pydantic schema, and `record()` reads back a
    json-ready row through `model_dump` instead of a hand-walked mapper column list. `TableBase`
    maps a real table declaratively (`table=True`) and `ViewBase` maps a read-only view
    imperatively onto its own `__view_select__`, but both reach the identical mapped `Table` seam
    and the identical `record()` contract from here.
    """

    # hybrid_property descriptors (FactClaim.is_current) carry no pydantic-core schema of their
    # own, so pydantic must be told to leave them alone rather than fail generating one.
    model_config = {"ignored_types": (hybrid_property,)}

    # columns no portable record carries: vectors re-derive from the stored text on import and
    # the generated lexical mirror is rebuilt by the schema, so both stay out of every dump.
    record_excluded: ClassVar[frozenset[str]] = frozenset({"embedding", "tsv"})

    # SQLAlchemy's declarative machinery sets this once the class is mapped, but sqlmodel never
    # types it, unlike SQLAlchemy's own `DeclarativeBase.__table__`; declaring it here gives every
    # concrete model a typed `cls.__table__.c` seam onto the real mapped `Column` objects, the one
    # place `FactClaim` reaches for it to sidestep the class-level `InstrumentedAttribute` gap on
    # a plain (non-`Mapped[...]`) `Field` column.
    __table__: ClassVar[Table]
    __tablename__: ClassVar[str]

    def record(self) -> dict[str, Json]:
        """Serialize any mapped row to a json-ready record tagged with its table name.

        `model_dump` walks pydantic's own field set, which for a mapped class is exactly its
        columns, relationships never among them, so every model serializes without hand-listing
        its fields. The tag key is `table` since a column named `kind` already means something on
        some rows.
        """
        return {"table": self.__tablename__} | self.model_dump(
            mode="json", exclude=set(self.record_excluded)
        )


class TableBase(MappedBase):
    """Declarative base shared by every aizk ORM table, auto-naming each table from its class."""

    @declared_attr.directive
    def __tablename__(cls) -> str:
        return derive_tablename(cls.__name__)

    # sqlmodel's own `__tablename__` is `ClassVar[str | Callable[..., str]]` in its annotation
    # but redefined in the same class body through a bare `@declared_attr`, so pyrefly's override
    # check compares against `declared_attr[Unknown]` rather than the annotation. SQLAlchemy's
    # own docs steer a non-Mapped directive like `__tablename__` to `.directive`, whose
    # `_declared_directive` class is a sibling of `declared_attr`, not a subtype, so the
    # documented API can never satisfy the override either way. The name has to stay
    # `__tablename__` (SQLAlchemy special-cases a dunder-named declared attribute to skip an
    # "unmanaged access" warning pre-mapping), so the seam is a same-object recast rather than a
    # renamed helper function.
    __tablename__ = cast("declared_attr[str]", __tablename__)
