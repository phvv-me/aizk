# The gliner sidecar's wire contract, shared by services/gliner/app.py (the FastAPI routes)
# and serving/gate/gliner.py (the client). The sidecar image copies this one file beside
# app.py, where it imports as a flat `contract` module, so it must depend on nothing beyond
# pydantic and the field names must stay exactly the wire format both sides already speak.

from pydantic import BaseModel, RootModel


class ClassifyTask(BaseModel):
    """One multi-label classification task schema GLiNER2 accepts."""

    labels: list[str]
    multi_label: bool = True
    cls_threshold: float | None = None


class ClassifyRequest(BaseModel):
    """A `/classify` call, the text and its named task schemas.

    A plain label list runs a task single-label while a `ClassifyTask` runs it
    multi-label under its own threshold, GLiNER2's two task spellings.
    """

    text: str
    tasks: dict[str, list[str] | ClassifyTask]


class ClassifyResponse(RootModel[dict[str, str | list[str] | None]]):
    """A `/classify` reply, one predicted label or label list per task."""

    def label(self, task: str) -> str | list[str] | None:
        """The prediction for one task, null when the model answered nothing for it."""
        return self.root.get(task)


class ExtractRequest(BaseModel):
    """An `/extract` call, the text and the entity types to score it against."""

    text: str
    entity_types: list[str]
    threshold: float = 0.5


class ExtractResponse(BaseModel):
    """An `/extract` reply, the found spans grouped by entity type."""

    entities: dict[str, list[str]] = {}


class ChunkRequest(BaseModel):
    """A `/chunk` call, the text and the chunker shape to split it with."""

    text: str
    kind: str = "text"
    chunk_size: int = 2048


class ChunkResponse(BaseModel):
    """A `/chunk` reply, the trimmed nonempty spans in order."""

    spans: list[str]


class HealthResponse(BaseModel):
    """A `/health` reply, liveness plus the device the weights landed on."""

    status: str
    device: str
