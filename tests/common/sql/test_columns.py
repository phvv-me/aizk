from datetime import datetime

import sqlalchemy as sa
from common.sql.model import Doc, compiled

from aizk.common.sql import Column


def test_column_is_a_runtime_eraser() -> None:
    """Subscripting the facade returns the annotation unchanged, so pydantic and
    SQLModel see the plain type."""
    assert Column[dict] is dict
    assert Column[list[float] | None] == list[float] | None


def test_column_instances_hold_plain_values() -> None:
    """Instance access is the validated value, class access is the column expression."""
    doc = Doc(title="a title", attributes={"summary": "text"}, created_at=datetime(2026, 1, 1))
    assert doc.title == "a title"
    assert doc.attributes == {"summary": "text"}
    assert compiled(Doc.title) == "doc.title"


def test_reader_casts_the_text_lookup_when_an_integer_is_requested() -> None:
    """`attributes[int] >> key` wraps the `->>` lookup in a cast to INTEGER."""
    expression = Doc.attributes[int] >> "count"
    assert compiled(expression) == "CAST(doc.attributes ->> 'count' AS INTEGER)"
    assert isinstance(expression.type, sa.Integer)
