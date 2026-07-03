from datetime import datetime
from typing import cast

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import DateTime, func
from sqlmodel import Field


def tz_datetime_field() -> datetime:
    """A timezone-aware timestamp column, server-stamped to the write time on insert."""
    return Field(
        default=None,
        nullable=False,
        sa_type=cast(type[datetime], DateTime(timezone=True)),
        sa_column_kwargs={"server_default": func.now()},
    )


def halfvec_field(dim: int) -> list[float] | None:
    """A nullable halfvec dense embedding column fixed to `dim`.

    dim: vector dimension the column is created with.
    """
    return Field(default=None, sa_type=cast(type[list[float]], HALFVEC(dim)))
