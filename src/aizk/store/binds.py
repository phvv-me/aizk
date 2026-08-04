from collections.abc import Iterable
from typing import cast

from pydantic import UUID5, UUID7
from sqlalchemy import ARRAY, ColumnElement, Text, Uuid, literal
from sqlalchemy.types import TypeEngine


def id_array(ids: Iterable[UUID5 | UUID7]) -> ColumnElement[list[UUID5]]:
    """Carry a set of ids into one statement as a single array parameter.

    A driver spends one parameter per element on an `IN` list and refuses a statement past
    32767 of them, which a graph pass reaches easily: a community rebuild names every entity
    its facts touch, and a large private scope has tens of thousands. One array is one
    parameter whatever its length, so `column = ANY(:ids)` has no ceiling to reach. The bind
    travels as text and casts inside the statement, the same shape every other array bind in
    the codebase already uses.
    """
    values = [str(value) for value in ids]
    return cast(
        "ColumnElement[list[UUID5]]",
        literal(values, cast("TypeEngine[list[str]]", ARRAY(Text))).cast(ARRAY(Uuid())),
    )
