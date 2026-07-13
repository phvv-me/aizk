from string.templatelib import Interpolation, Template

from sqlalchemy import ColumnElement, func, literal


def concat(template: Template) -> ColumnElement[str]:
    """Concatenate a t-string's literals and column interpolations into one SQL string.

    Template iteration already omits empty literals, so only real fragments become
    arguments. SQL `concat` skips NULL arguments, so an optional fragment arrives
    pre-coalesced when an empty string must appear instead of erasing its neighbors.
    """
    parts = [part.value if isinstance(part, Interpolation) else part for part in template]
    return func.concat(*parts)


def fragment(template: Template) -> ColumnElement[str]:
    """An optional t-string fragment: a NULL interpolation erases the whole piece.

    `||` propagates NULL where concat would swallow it, and the coalesce renders the
    erased fragment as the empty string.
    """
    parts = [part.value if isinstance(part, Interpolation) else part for part in template]
    joined = literal(parts[0]) if isinstance(parts[0], str) else parts[0]
    for part in parts[1:]:
        joined = joined.concat(part)
    return func.coalesce(joined, "")
