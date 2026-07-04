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


class BatchConsolidationVerdict(FrozenModel):
    """One ADD/UPDATE/NOOP verdict per borderline fact in a batch, aligned by position.

    The non-LLM consolidation cascade defers here only for the facts whose top similar claim
    landed in the ambiguous cosine band; every batch this model answers covers a whole chunk's
    borderline facts in the one call `extract.llm.decide_consolidations_batch` makes, never one
    call per fact.

    verdicts: the resolved actions, in the same order the borderline facts were listed.
    """

    verdicts: list[ConsolidationVerdict]
