from typing import ClassVar

from sqlalchemy import Index
from sqlalchemy.orm import declared_attr

from ...config import settings
from .fields import halfvec_field


class Embedded:
    """A halfvec dense embedding column, null until embedded, with its own cosine ANN index.

    A subclass declaring extra `__table_args__` composes this one with `*super().__table_args__`
    rather than re-listing the embedding index, so the ann lane never drifts per table.

    embedding: halfvec dense vector, null until embedded.
    """

    # a bare annotation, never assigned here: every concrete table mixes `Embedded` alongside
    # `TableBase`, whose own `declared_attr` supplies the real value, so this only states the one
    # attribute `__table_args__` below reaches for. Typing `cls` against `TableBase` itself trips
    # pyrefly's self-type check, since `Embedded` is not one of its subclasses.
    __tablename__: ClassVar[str]

    embedding: list[float] | None = halfvec_field(settings.embed_dim)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index, ...]:
        table = cls.__tablename__
        return (
            Index(
                f"ix_{table}_embedding",
                "embedding",
                postgresql_using=settings.index_backend,
                postgresql_ops={"embedding": "halfvec_cosine_ops"},
            ),
        )
