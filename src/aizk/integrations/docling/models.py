import unicodedata
from pathlib import Path
from typing import Annotated, Literal, cast

from patos import FrozenModel
from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)


def _has_control(value: str) -> bool:
    """Whether the string carries any C0, DEL, or C1 control character (Unicode category Cc)."""
    return any(unicodedata.category(character) == "Cc" for character in value)


def _component(value: str) -> str:
    """Reject path components and control characters from a suggested artifact name."""
    if Path(value).name != value:
        raise ValueError("filename must be one safe path component")
    if "\\" in value or _has_control(value):
        raise ValueError("filename contains an unsafe character")
    return value


def _header_safe(value: str) -> str:
    """Reject control characters that could escape a multipart content-type header."""
    if _has_control(value):
        raise ValueError("media_type contains an unsafe character")
    return value


type Filename = Annotated[
    str,
    StringConstraints(min_length=1, max_length=255),
    AfterValidator(_component),
]
type MediaType = Annotated[str, StringConstraints(max_length=255), AfterValidator(_header_safe)]


class FileSource(FrozenModel):
    """One local artifact inside the configured conversion staging root."""

    kind: Literal["file"] = "file"
    path: Path
    filename: Filename | None = None
    media_type: MediaType | None = None


class URISource(FrozenModel):
    """One public HTTPS artifact fetched through the guarded source reader."""

    kind: Literal["uri"] = "uri"
    uri: AnyHttpUrl
    filename: Filename | None = None
    media_type: MediaType | None = None

    @field_validator("uri")
    @classmethod
    def require_public_transport(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Require HTTPS and reject credentials before any network operation begins."""
        if value.scheme != "https":
            raise ValueError("remote artifacts require HTTPS")
        if value.username is not None or value.password is not None:
            raise ValueError("remote artifact URIs cannot contain credentials")
        return value


type ArtifactSource = Annotated[FileSource | URISource, Field(discriminator="kind")]


class ArtifactBytes(FrozenModel):
    """One bounded artifact body ready for the internal conversion service."""

    content: bytes
    filename: Filename
    media_type: MediaType


class DoclingOptions(FrozenModel):
    """Declare the bounded conversion policy sent to Docling Serve.

    Image export and output formats remain architectural invariants. Docling may use images while
    converting, but AIZK receives placeholders and stores only JSON and Markdown derivatives.
    """

    pipeline: Literal["standard", "vlm"] = "standard"
    do_ocr: bool = True
    force_ocr: bool = False
    table_mode: Literal["fast", "accurate"] = "accurate"
    code_enrichment: bool = False
    formula_enrichment: bool = False
    picture_classification: bool = False
    chart_extraction: bool = False
    picture_description: bool = False
    picture_description_preset: str = "default"
    document_timeout: float = 1800.0

    def form_data(self) -> dict[str, str | list[str]]:
        """Serialize the supported stable v1 options as multipart form values."""
        values: dict[str, str | list[str]] = {
            "to_formats": ["json", "md"],
            "image_export_mode": "placeholder",
            "pipeline": self.pipeline,
            "do_ocr": str(self.do_ocr).lower(),
            "force_ocr": str(self.force_ocr).lower(),
            "table_mode": self.table_mode,
            "do_code_enrichment": str(self.code_enrichment).lower(),
            "do_formula_enrichment": str(self.formula_enrichment).lower(),
            "do_picture_classification": str(self.picture_classification).lower(),
            "do_chart_extraction": str(self.chart_extraction).lower(),
            "do_picture_description": str(self.picture_description).lower(),
            "document_timeout": str(self.document_timeout),
        }
        if self.picture_description:
            values["picture_description_preset"] = self.picture_description_preset
        return values


class DoclingDocument(FrozenModel):
    """The lossless structure and normalized text requested from Docling Serve."""

    md_content: str | None = None
    json_content: dict[str, JsonValue] | None = None


class DoclingResponse(FrozenModel):
    """Typed single-document response returned by Docling Serve's stable v1 API."""

    document: DoclingDocument
    status: Literal["success", "partial_success", "skipped", "failure"]
    processing_time: float = 0.0
    timings: dict[str, JsonValue] = {}
    errors: list[dict[str, JsonValue]] = []

    @property
    def markdown(self) -> str:
        """Return deterministic NFC Markdown with Unix line endings and one final newline."""
        text = unicodedata.normalize("NFC", self.document.md_content or "")
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        normalized = "\n".join(line.rstrip() for line in lines).strip()
        return f"{normalized}\n" if normalized else ""

    @property
    def native_json(self) -> dict[str, JsonValue]:
        """Return Docling's complete native document tree."""
        return self.document.json_content or {}

    @property
    def details(self) -> dict[str, JsonValue]:
        """Return conversion diagnostics separately from the native document tree."""
        return cast(
            dict[str, JsonValue],
            self.model_dump(mode="json", exclude={"document"}),
        )


class DoclingConversionError(RuntimeError):
    """Docling finished without producing a usable lossless conversion."""


class DoclingOutput(FrozenModel):
    """Lossless native JSON and normalized Markdown ready for an artifact byte sink."""

    status: Literal["success", "partial_success"]
    docling_json: dict[str, JsonValue]
    markdown: str
    details: dict[str, JsonValue]

    @classmethod
    def from_response(cls, response: DoclingResponse) -> DoclingOutput:
        """Accept complete or partial output and reject skipped, failed, or missing formats."""
        if response.status not in ("success", "partial_success"):
            raise DoclingConversionError(f"Docling conversion ended with {response.status}")
        if response.document.json_content is None or response.document.md_content is None:
            raise DoclingConversionError("Docling response omitted JSON or Markdown")
        return cls(
            status=response.status,
            docling_json=response.native_json,
            markdown=response.markdown,
            details=response.details,
        )
