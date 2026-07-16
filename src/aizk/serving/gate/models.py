# The GLiNER sidecar imports this module as a flat `models` file. It therefore depends only on
# Pydantic and keeps the exact wire field names shared with the Aizk client.

from pydantic import BaseModel, Field, RootModel


class ClassifyTask(BaseModel):
    """One multi-label classification task schema GLiNER accepts."""

    labels: list[str]
    multi_label: bool = True
    cls_threshold: float | None = None


class ClassifyRequest(BaseModel):
    """Text and named task schemas for one classification call."""

    text: str
    tasks: dict[str, list[str] | ClassifyTask]


class ClassifyResponse(RootModel[dict[str, str | list[str] | None]]):
    """One prediction per requested task."""

    def label(self, task: str) -> str | list[str] | None:
        """Return one task prediction when present."""
        return self.root.get(task)


class ExtractRequest(BaseModel):
    """Text and entity types for one mention extraction call."""

    text: str
    entity_types: list[str]
    threshold: float = 0.5


class ExtractResponse(BaseModel):
    """Mention spans grouped by entity type."""

    entities: dict[str, list[str]] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """Sidecar liveness and model device."""

    status: str
    device: str
    checkpoint: str
