from sqlalchemy.schema import ExecutableDDLElement


class DropView(ExecutableDDLElement):
    """Drop a PostgreSQL view when present."""

    inherit_cache = False

    def __init__(self, name: str) -> None:
        self.name = name
