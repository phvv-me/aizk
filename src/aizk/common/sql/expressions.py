from datetime import datetime
from typing import cast as typing_cast

from sqlalchemy import ColumnElement, Float, extract, func, null, type_coerce


def provided[T](column: ColumnElement[T] | None) -> ColumnElement[T]:
    """An optional column resolved against its typed NULL.

    column: the column expression a caller supplies, or None where it supplies none.
    """
    if column is None:
        return typing_cast(ColumnElement[T], null())
    return column


def days_since(
    timestamp: ColumnElement[datetime] | ColumnElement[datetime | None],
) -> ColumnElement[float]:
    """Fractional days from a timestamp to the database clock, a ratio of epoch seconds.

    The day length comes from interval arithmetic the planner folds to a constant, never
    a bare 86400.
    """
    one_day = func.make_interval(0, 0, 0, 1)
    age = extract("epoch", func.now() - timestamp) / extract("epoch", one_day)
    return type_coerce(age, Float)
