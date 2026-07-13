import uuid
from typing import Literal

from patos import FrozenModel


class ConsolidationVerdict(FrozenModel):
    """The decision on how a new fact relates to the existing latest facts."""

    action: Literal["ADD", "UPDATE", "NOOP"]
    supersedes: uuid.UUID | None = None


class BatchConsolidationVerdict(FrozenModel):
    """One ADD/UPDATE/NOOP verdict per borderline fact in a batch, aligned by position."""

    verdicts: list[ConsolidationVerdict]
