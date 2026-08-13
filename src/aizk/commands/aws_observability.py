from functools import cache

from ..store.engine import Database
from ..usage import observe


@cache
def instrument(database: Database) -> None:
    """Install process tracing once while Lambda reuses a warm execution environment."""
    observe(database)
