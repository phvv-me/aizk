from typing import ClassVar, cast

from patos import sql
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.orm import declared_attr
from sqlmodel import Field

from ...config import settings


class Embedded:
    """Nullable halfvec embedding with a cosine ANN index."""

    __tablename__: ClassVar[str]

    embedding: sql.Column[list[float] | None] = Field(
        default=None, sa_type=cast(type[list[float]], sql.CosineHalfvec(settings.embed_dim))
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | UniqueConstraint, ...]:
        table = cls.__tablename__
        return (
            Index(
                f"ix_{table}_embedding",
                "embedding",
                postgresql_using=settings.index_backend,
                postgresql_ops={"embedding": "halfvec_cosine_ops"},
            ),
        )
