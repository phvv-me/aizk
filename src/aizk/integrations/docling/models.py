import unicodedata
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from patos import FrozenModel, FrozenOpenModel
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

    Output format remains an architectural invariant. Image export is embedded only when the
    text-description lane needs figure bytes, and those data URIs are replaced before the
    derivative is stored. The native document tree is not requested because nothing reads it.

    The OCR engine and its languages are always declared. Docling's automatic choice is RapidOCR,
    whose bundled recognition model is Chinese and which has no Japanese model set at all, so a
    scanned Japanese page came back as plausible but wrong CJK that nothing downstream could
    detect. Tesseract reads the languages it is given, which makes the request the only place
    that decides what a page is read as.
    """

    pipeline: Literal["standard", "vlm"] = "standard"
    image_export_mode: Literal["placeholder", "embedded"] = "placeholder"
    do_ocr: bool = True
    force_ocr: bool = False
    ocr_engine: str = "tesseract"
    ocr_languages: Annotated[tuple[str, ...], Field(min_length=1)] = ("jpn", "eng")
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
            "to_formats": ["md"],
            "image_export_mode": self.image_export_mode,
            "pipeline": self.pipeline,
            "do_ocr": str(self.do_ocr).lower(),
            "force_ocr": str(self.force_ocr).lower(),
            # `ocr_preset` is the current field and `ocr_engine` the deprecated twin it replaced,
            # so both carry the same value and every Docling Serve generation reads the one it
            # knows. Sending neither is what let the Chinese default model read Japanese pages.
            "ocr_engine": self.ocr_engine,
            "ocr_preset": self.ocr_engine,
            "ocr_lang": list(self.ocr_languages),
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


class DoclingDocument(FrozenOpenModel):
    """The normalized text requested from Docling Serve."""

    md_content: str | None = None


class DoclingErrorItem(FrozenOpenModel):
    """One structured error Docling attributes to a category.

    The category is what separates a deterministic policy refusal, `policy`, from a
    transient processing failure, so it is the signal a caller classifies on rather than
    the human-readable `error_message` beside it. It stays a plain string rather than a
    closed enum because Docling documents `unknown` as its own fallback for a category it
    has not named yet, and a stricter type would fail to parse the exact response this
    field exists to classify.
    """

    category: str = "unknown"
    error_message: str = ""


class DoclingResponse(FrozenOpenModel):
    """Typed single-document response returned by Docling Serve's stable v1 API."""

    document: DoclingDocument
    status: Literal["success", "partial_success", "skipped", "failure"]
    processing_time: float = 0.0
    timings: dict[str, JsonValue] = {}
    errors: list[DoclingErrorItem] = []

    @property
    def markdown(self) -> str:
        """Return deterministic NFC Markdown with Unix line endings and one final newline."""
        text = unicodedata.normalize("NFC", self.document.md_content or "")
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        normalized = "\n".join(line.rstrip() for line in lines).strip()
        return f"{normalized}\n" if normalized else ""

    @property
    def policy_rejection(self) -> str | None:
        """Docling's own policy explanation for refusing this input, or nothing when none applies.

        Every error Docling categorizes `policy` reflects a decision its user-input validation
        made from the bytes and filename alone, before any conversion was attempted, so the
        exact same request would reach the exact same verdict on every retry. That determinism
        is what makes it safe to treat as permanent instead of requeuing it forever.
        """
        reasons = [error.error_message for error in self.errors if error.category == "policy"]
        if not reasons:
            return None
        return (
            "; ".join(reason for reason in reasons if reason)
            or "Docling policy refused this input"
        )


class DoclingConversionError(RuntimeError):
    """Docling finished without producing a usable lossless conversion."""


class DoclingUnreadableFormatError(DoclingConversionError):
    """Docling's own policy check permanently refused this input, a verdict retrying repeats."""


class DoclingOutput(FrozenModel):
    """Normalized Markdown ready for an artifact byte sink."""

    status: Literal["success", "partial_success"]
    markdown: str

    @classmethod
    def from_response(cls, response: DoclingResponse) -> DoclingOutput:
        """Accept complete or partial output and reject skipped, failed, or missing Markdown."""
        rejection = response.policy_rejection
        if rejection is not None:
            raise DoclingUnreadableFormatError(rejection)
        if response.status not in ("success", "partial_success"):
            raise DoclingConversionError(f"Docling conversion ended with {response.status}")
        if response.document.md_content is None:
            raise DoclingConversionError("Docling response omitted Markdown")
        return cls(status=response.status, markdown=response.markdown)


# Docling Serve resolves the input format from the filename's extension, never from the
# declared content type, so a display name without one, or with the wrong one, turns a
# document Docling can read into a `skipped` policy refusal instead of a conversion. Each
# entry names the extension Docling's own format router recognizes for one media type AIZK
# accepts, read directly from the pinned `docling-serve` image's `FormatToExtensions` table.
_DOCLING_EXTENSIONS: dict[str, str] = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/tiff": "tiff",
    "image/bmp": "bmp",
    "image/webp": "webp",
    "application/epub+zip": "epub",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/markdown": "md",
    "text/plain": "txt",
    "text/asciidoc": "adoc",
    "text/csv": "csv",
}


def docling_filename(filename: str, media_type: str) -> str:
    """Rename the copy of `filename` sent to Docling with the extension its router expects.

    Only the wire copy changes, never the artifact's own stored display name. The stem is
    kept so the sent name still reads as the original, and a media type outside this table
    passes through unchanged rather than guessing at an extension Docling was never confirmed
    to recognize.
    """
    extension = _DOCLING_EXTENSIONS.get(media_type)
    if extension is None:
        return filename
    return f"{PurePosixPath(filename).stem}.{extension}"
