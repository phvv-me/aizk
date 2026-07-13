from sqlalchemy.schema import ExecutableDDLElement


class CreateExtension(ExecutableDDLElement):
    """Create a PostgreSQL extension when absent."""

    inherit_cache = False

    def __init__(self, name: str) -> None:
        self.name = name
