from collections.abc import Sequence

from patos import FrozenModel


class FAMAScore(FrozenModel):
    """Forgetting-Aware Memory Accuracy and its auditable component scores."""

    memory_presence_accuracy: float
    forgetting_absence_accuracy: float
    forgetting_weight: float
    score: float

    @classmethod
    def from_judgments(
        cls,
        memory_presence: Sequence[bool],
        forgetting_absence: Sequence[bool] = (),
    ) -> FAMAScore:
        """Compute the Memora paper's per-question score from binary criterion judgments."""
        if not memory_presence:
            raise ValueError("FAMA requires at least one memory-presence criterion")
        presence_accuracy = sum(memory_presence) / len(memory_presence)
        absence_accuracy = (
            sum(forgetting_absence) / len(forgetting_absence) if forgetting_absence else 1.0
        )
        weight = len(forgetting_absence) / (len(memory_presence) + len(forgetting_absence))
        return cls(
            memory_presence_accuracy=presence_accuracy,
            forgetting_absence_accuracy=absence_accuracy,
            forgetting_weight=weight,
            score=max(0.0, presence_accuracy - weight * (1.0 - absence_accuracy)),
        )
