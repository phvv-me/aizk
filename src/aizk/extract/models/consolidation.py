import uuid
from typing import Literal

from patos import FrozenModel


class ConsolidationVerdict(FrozenModel):
    """The decision on how a new fact relates to the existing latest facts.

    action: ADD a genuinely new fact, UPDATE one that supersedes an old fact, or NOOP a duplicate.
    supersedes: id of the fact the new one retires, set only when action is UPDATE.
    """

    action: Literal["ADD", "UPDATE", "NOOP"]
    supersedes: uuid.UUID | None = None
