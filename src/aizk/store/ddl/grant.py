from sqlalchemy.schema import ExecutableDDLElement

from .grant_target import GrantTarget


class Grant(ExecutableDDLElement):
    """A typed privilege grant with quoted identifiers."""

    inherit_cache = False

    def __init__(
        self,
        target: GrantTarget,
        name: str,
        role: str,
        privileges: tuple[str, ...],
    ) -> None:
        self.grant_target = target
        self.name = name
        self.role = role
        self.privileges = privileges
