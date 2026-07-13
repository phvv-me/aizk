from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Float, Text
from sqlalchemy.dialects.postgresql import JSONB

from .columns import JSONReader


class TypedJSONB(JSONB):
    """JSONB whose `>>` reads one key as text and whose `[kind]` yields a casting reader."""

    cache_ok = True

    class Comparator(JSONB.Comparator):
        def __getitem__(self, index: Any) -> Any:
            if isinstance(index, type):
                return JSONReader(self.expr, index)
            return super().__getitem__(index)

        def __rshift__(self, other: str) -> Any:
            return self.expr.op("->>", return_type=Text)(other)

    comparator_factory = Comparator


class CosineHalfvec(HALFVEC):
    """halfvec whose `@` is the cosine distance operator `<=>` its indexes order by."""

    cache_ok = True

    class Comparator(HALFVEC.Comparator):
        def __matmul__(self, other: Any) -> Any:
            return self.expr.op("<=>", return_type=Float)(other)

    comparator_factory = Comparator
