from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ColumnElement, Float, Integer, Text
from sqlalchemy import cast as sql_cast
from sqlalchemy.types import TypeEngine

CASTS: dict[type, type[TypeEngine[Any]]] = {int: Integer, float: Float, bool: Boolean}


class JSONReader[V]:
    """Reads JSONB keys under one Python type, so `column[int] >> key` casts the text.

    column: the JSONB column expression the readings come from.
    kind: the Python type whose SQL cast wraps each reading.
    """

    def __init__(self, column: ColumnElement[Any], kind: type[V]) -> None:
        self.column = column
        self.kind = kind

    def __rshift__(self, key: str) -> ColumnElement[V]:
        """The `->>` text reading of `key` cast to this reader's SQL type."""
        reading = self.column.op("->>", return_type=Text)(key)
        return sql_cast(reading, CASTS[self.kind])


if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import overload

    class Expr[T](ColumnElement[T]):
        """What a `Column[T]` attribute is at class level, the instrumented column
        expression with the house operators typed."""

        @overload
        def __getitem__[V: (int, float, bool)](self, index: type[V]) -> JSONReader[V]: ...
        @overload
        def __getitem__(self, index: Any) -> ColumnElement[Any]: ...
        def __getitem__(self, index: Any) -> Any: ...

        def __rshift__(self, other: str) -> ColumnElement[str]: ...

        def __matmul__(
            self, other: Sequence[float] | ColumnElement[Any] | Expr[Any]
        ) -> ColumnElement[float]: ...

    class Column[T]:
        """Annotation facade, instance access is the value and class access is `Expr[T]`."""

        @overload
        def __get__(self, instance: None, owner: Any = None) -> Expr[T]: ...
        @overload
        def __get__(self, instance: object, owner: Any = None) -> T: ...
        def __get__(self, instance: Any, owner: Any = None) -> Any: ...

        def __set__(self, instance: Any, value: T | Expr[T]) -> None: ...

else:

    class Column:
        """Runtime eraser, `Column[X]` is exactly `X`, so pydantic validation, SQLModel
        column inference and serialization see the plain annotation."""

        def __class_getitem__(cls, item: Any) -> Any:
            return item
