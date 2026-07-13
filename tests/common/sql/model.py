from datetime import datetime
from typing import cast

from sqlalchemy import ColumnElement
from sqlalchemy.dialects import postgresql
from sqlmodel import Field, SQLModel

from aizk.common.sql import Column, CosineHalfvec, TypedJSONB


class Doc(SQLModel, table=True):
    id: Column[int] = Field(default=None, primary_key=True)
    title: Column[str]
    summary: Column[str | None] = Field(default=None)
    attributes: Column[dict] = Field(default_factory=dict, sa_type=TypedJSONB)
    embedding: Column[list[float] | None] = Field(
        default=None, sa_type=cast(type[list[float]], CosineHalfvec(3))
    )
    created_at: Column[datetime]


def compiled[T](expression: ColumnElement[T]) -> str:
    """Compile one expression against the PostgreSQL dialect with inline literals."""
    return str(
        expression.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
