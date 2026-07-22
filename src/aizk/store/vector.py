from collections.abc import Sequence
from typing import cast

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import ColumnElement, Float


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


def cosine_distance[L: Sequence[float] | None, R: Sequence[float]](
    left: ColumnElement[L],
    right: R | ColumnElement[R],
) -> ColumnElement[float]:
    """Build portable cosine distance without relying on SQLAlchemy operator forwarding."""
    return cast(ColumnElement[float], left.op("<=>", return_type=Float)(right))
