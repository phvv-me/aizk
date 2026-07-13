import uuid
from datetime import datetime
from enum import StrEnum, auto

from patos import FrozenModel
from pydantic.types import JsonValue


class EpistemicKind(StrEnum):
    """How a claim relates to a speaker and the shared world."""

    world = auto()
    experience = auto()
    observation = auto()
    opinion = auto()
    preference = auto()
    procedure = auto()
    negative_result = auto()

    @property
    def speaker_bound(self) -> bool:
        """Whether two speakers may hold distinct versions of this kind."""
        return self in {
            self.experience,
            self.observation,
            self.opinion,
            self.preference,
        }

    def perspective_key(self, created_by: uuid.UUID) -> str:
        """Return the consolidation partition for this kind and creator."""
        return f"speaker:{created_by}" if self.speaker_bound else "world"


class CaptureContext(FrozenModel):
    """Portable speaker and conversation context captured with one source span."""

    speaker_label: str | None = None
    speaker_role: str | None = None
    channel: str | None = None
    reply_to: str | None = None
    phase: str | None = None
    topic: str | None = None
    observed_at: datetime | None = None

    def record(self) -> dict[str, JsonValue]:
        """Render non-null fields for JSONB storage."""
        return self.model_dump(mode="json", exclude_none=True)

    def search_text(self, text: str) -> str:
        """Prefix text with speaker and conversation terms for embedding and lexical search."""
        context = [
            value
            for value in (
                f"speaker {self.speaker_label}" if self.speaker_label else None,
                f"role {self.speaker_role}" if self.speaker_role else None,
                f"channel {self.channel}" if self.channel else None,
                f"phase {self.phase}" if self.phase else None,
                f"topic {self.topic}" if self.topic else None,
            )
            if value is not None
        ]
        return "\n".join([*context, text]) if context else text

    def claim_attributes(self, kind: EpistemicKind, created_by: uuid.UUID) -> dict[str, JsonValue]:
        """Build fact attributes that preserve epistemic and speaker provenance."""
        return self.record() | {
            "epistemic_kind": kind.value,
            "perspective_key": kind.perspective_key(created_by),
        }
