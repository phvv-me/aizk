from collections.abc import Sequence
from typing import cast

from patos.sql import CosineHalfvec
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import ColumnElement, Float, FromClause

from ..config import DatabaseBackend, settings


class CosineVector(VECTOR):
    """Portable vector whose `@` operator is cosine distance."""

    cache_ok = True
    render_bind_cast = True

    class Comparator(VECTOR.Comparator):
        def __matmul__(
            self,
            other: Sequence[float] | ColumnElement[Sequence[float]],
        ) -> ColumnElement[float]:
            return cast(ColumnElement[float], self.expr.op("<=>", return_type=Float)(other))

    comparator_factory = Comparator


def embedding_vector(dimensions: int) -> CosineHalfvec | CosineVector:
    """The embedding column and bind type for the active backend.

    PostgreSQL stores half vectors, halving embedding bytes and index size, and every lane
    bind must carry the same type, or the planner casts the column side of the distance
    comparison and the index walk degrades to a scan. CockroachDB has no halfvec, so the
    portable backend keeps full vectors.
    """
    if settings.database_backend is DatabaseBackend.cockroachdb:
        return CosineVector(dimensions)
    return CosineHalfvec(dimensions)


def cosine_distance[L: Sequence[float] | None, R: Sequence[float]](
    left: ColumnElement[L],
    right: R | ColumnElement[R],
) -> ColumnElement[float]:
    """Build portable cosine distance without relying on SQLAlchemy operator forwarding."""
    return cast(ColumnElement[float], left.op("<=>", return_type=Float)(right))


def embedding_column(
    source: FromClause, name: str = "embedding"
) -> ColumnElement[Sequence[float]]:
    """Read one vector column off a `values()` construct as the vector it was declared with.

    A `values()` column carries the union of every type the construct can hold rather than the
    one its `column()` declared, so the vector type is restored here instead of at each caller.
    """
    return cast(ColumnElement[Sequence[float]], source.c[name])
