from patos import sql
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.orm import declared_attr

from ...config import settings
from ..vector import CosineVector


class Embedded(sql.Model):
    """Nullable portable vector embedding with a cosine ANN index."""

    embedding = sql.Field(
        list[float] | None,
        sa_type=CosineVector(settings.embed_dim),
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | UniqueConstraint, ...]:
        table = cls.__tablename__
        return (
            Index(
                f"ix_{table}_embedding",
                "embedding",
                postgresql_using=settings.vector_index_backend,
                postgresql_ops={"embedding": "vector_cosine_ops"},
            ),
        )
