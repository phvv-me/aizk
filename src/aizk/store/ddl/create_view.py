from sqlalchemy.schema import ExecutableDDLElement
from sqlalchemy.sql import Select


class CreateView(ExecutableDDLElement):
    """Create a security-invoker PostgreSQL view from a typed select.

    SQLAlchemy 2.1 ships a native `CreateView`, but as of 2.1.0b3 it cannot express this
    statement. Its `visit_create_view` renders no view options at all and the postgresql
    dialect declares no construct arguments for it, so `postgresql_with` keyword arguments
    raise `ArgumentError` instead of compiling. This element stays custom until the native
    construct can emit `WITH (security_invoker = true)`.
    """

    inherit_cache = False

    def __init__(self, name: str, select: Select) -> None:
        self.name = name
        self.select = select
