from patos import sql
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.orm import declared_attr

from ...config import settings


class Embedded(sql.Model):
    """Nullable halfvec embedding with a cosine ANN index."""

    embedding = sql.Field(
        list[float] | None,
        sa_type=sql.CosineHalfvec(settings.embed_dim),
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
