import sqlalchemy as sa
from common.sql.model import Doc, compiled

from aizk.common import sql


def test_provided_resolves_an_optional_column_against_a_typed_null() -> None:
    """A supplied column passes through untouched and an absent one renders as NULL."""
    assert sql.provided(Doc.title) is Doc.title
    assert compiled(sql.provided(None)) == "NULL"


def test_days_since_is_a_ratio_of_epoch_seconds() -> None:
    """Age divided by one interval day, both through EXTRACT(epoch ...)."""
    expression = sql.days_since(Doc.created_at)
    assert compiled(expression) == (
        "EXTRACT(epoch FROM now() - doc.created_at)"
        " / CAST(EXTRACT(epoch FROM make_interval(0, 0, 0, 1)) AS NUMERIC)"
    )
    assert isinstance(expression.type, sa.Float)
