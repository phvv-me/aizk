from datetime import datetime
from typing import Annotated

from patos import FrozenModel
from pydantic import UUID5, UUID7, UUID8, AfterValidator, JsonValue

from ..common.templates import markdown_environment
from ..store import Artifact, Blob
from ..types import Scopes

_template = markdown_environment("aizk.artifacts").get_template("source.md.j2")


def blank_to_none(value: str | None) -> str | None:
    """Strip surrounding whitespace and collapse a blank string to an absent value."""
    if value is not None:
        value = value.strip()
    return value or None


type Prose = Annotated[str | None, AfterValidator(blank_to_none)]


class ArtifactReceipt(FrozenModel):
    """Identify one accepted original and its asynchronous processing state."""

    artifact_id: UUID7
    content_id: UUID7
    state: Artifact.Content.State


class OriginalDescription(FrozenModel):
    """Caller-supplied identity and context accompanying one accepted original."""

    filename: str
    media_type: str
    source_uri: str | None = None
    companion_text: Prose = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None


class IntegrityReport(FrozenModel):
    """Summarize one bounded object-store integrity pass."""

    checked: int
    valid: int
    failed: int


class OriginalArtifact(FrozenModel):
    """Authorize and materialize the immutable original needed by one conversion job."""

    artifact_id: UUID7
    content_id: UUID7
    revision: int
    created_by: UUID5
    scopes: Scopes
    filename: str
    media_type: str
    size: int
    source_uri: str | None
    companion_text: Prose = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    storage_key: str
    storage_version: str | None = None
    storage_hash: UUID8
    storage_encoding: Blob.Encoding = Blob.Encoding.identity


class ArtifactDocument(FrozenModel):
    """Render one file revision into deterministic source text for recall."""

    filename: str
    media_type: str
    size: int
    source_uri: str | None = None
    companion_text: Prose = None
    markdown: Prose = None
    conversion_state: Artifact.Content.State
    details: dict[str, JsonValue] = {}

    @property
    def semantic(self) -> bool:
        """Whether the source contains authored or successfully extracted content."""
        return bool(self.companion_text or self.markdown)

    async def to_markdown(self) -> str:
        """Keep companion content, file identity, conversion state, and extracted text together."""
        return await _template.render_async(document=self)
