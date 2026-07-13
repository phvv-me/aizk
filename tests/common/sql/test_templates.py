from common.sql.model import Doc, compiled

from aizk.common import sql


def test_concat_interleaves_literals_and_columns() -> None:
    """Literals and column interpolations become concat arguments in template order."""
    title = Doc.title
    summary = Doc.summary
    expression = sql.concat(t"title: {title}, summary: {summary}")
    assert compiled(expression) == ("concat('title: ', doc.title, ', summary: ', doc.summary)")


def test_fragment_is_a_null_propagating_chain_inside_coalesce() -> None:
    """Literals and columns join through `||`, so one NULL erases the piece, and the
    coalesce renders the erased fragment as the empty string."""
    summary = Doc.summary
    expression = sql.fragment(t"summary: {summary}")
    assert compiled(expression) == "coalesce('summary: ' || doc.summary, '')"


def test_fragment_chains_adjacent_interpolations_without_a_leading_literal() -> None:
    """An interpolation-first fragment chains directly, with no empty literal pieces."""
    title = Doc.title
    summary = Doc.summary
    expression = sql.fragment(t"{title}{summary}")
    assert compiled(expression) == "coalesce(doc.title || doc.summary, '')"
