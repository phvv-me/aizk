import asyncio
import json
from importlib.metadata import distribution
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Protocol

from liteparse import LiteParse, PageComplexityStats, ParseError
from loguru import logger

from .docling import (
    ArtifactBytes,
    DoclingConversionError,
    DoclingOptions,
    DoclingResponse,
)
from .docling.models import docling_filename


class ArtifactConverter(Protocol):
    """Convert one bounded artifact to the normalized response the processor consumes."""

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        """Return one Markdown derivative or raise a conversion failure."""
        ...


def response(markdown: str, started_at: float) -> DoclingResponse:
    """Wrap a local converter result in the existing normalized response contract."""
    return DoclingResponse.model_validate(
        {
            "document": {"md_content": markdown},
            "status": "success",
            "processing_time": perf_counter() - started_at,
        }
    )


class LiteParseConverter:
    """Read simple native PDFs cheaply and expose explicit escalation reasons."""

    def __init__(self) -> None:
        self.parser = LiteParse(
            output_format="markdown",
            include_complexity=True,
            keep_headers_footers=False,
        )

    async def inspect(self, artifact: ArtifactBytes) -> tuple[str, ...]:
        """Return OCR or layout reasons that require the heavy converter."""
        try:
            pages = await asyncio.to_thread(self.parser.is_complex, artifact.content)
        except ParseError as error:
            return (f"liteparse-error {error}",)
        return tuple(reason for page in pages for reason in self.reasons(page))

    def reasons(self, page: PageComplexityStats) -> tuple[str, ...]:
        """Combine OCR and layout signals for one page without hiding their source."""
        reasons = list(page.reasons)
        if page.layout is not None:
            reasons.extend(page.layout.reasons)
        return tuple(dict.fromkeys(reasons))

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        """Extract one simple PDF as Markdown without loading model weights."""
        started_at = perf_counter()
        try:
            parsed = await asyncio.to_thread(self.parser.parse, artifact.content)
        except ParseError as error:
            raise DoclingConversionError(
                f"LiteParse could not read the PDF because {error}"
            ) from error
        return response(parsed.text, started_at)


class PandocConverter:
    """Convert Office, EPUB, and web markup with a short-lived Pandoc process."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.executable = self.resolve_executable()

    def resolve_executable(self) -> str:
        """Find the Pandoc binary shipped by the pinned runtime wheel."""
        package = distribution("pypandoc-binary")
        for file in package.files or ():
            path = Path(file)
            if path.parent.name == "files" and path.name in {"pandoc", "pandoc.exe"}:
                return str(package.locate_file(file))
        raise RuntimeError("pypandoc-binary did not contain a Pandoc executable")

    def input_format(self, media_type: str) -> str | None:
        """Map accepted media types to the exact Pandoc reader name."""
        return {
            "application/epub+zip": "epub",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
            "application/xhtml+xml": "html",
            "text/asciidoc": "asciidoc",
            "text/html": "html",
        }.get(media_type)

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        """Stream one artifact through Pandoc and collect bounded Markdown output."""
        input_format = self.input_format(artifact.media_type)
        if input_format is None:
            raise DoclingConversionError(f"Pandoc has no route for {artifact.media_type}")
        started_at = perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "--from",
                input_format,
                "--to",
                "gfm",
                "--wrap",
                "none",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise DoclingConversionError(f"Pandoc could not start because {error}") from error
        try:
            async with asyncio.timeout(self.timeout):
                output, errors = await process.communicate(artifact.content)
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise DoclingConversionError("Pandoc conversion timed out") from error
        if process.returncode:
            message = errors.decode("utf-8", "replace").strip()
            raise DoclingConversionError(f"Pandoc conversion failed because {message}")
        return response(output.decode("utf-8"), started_at)


class TextConverter:
    """Preserve accepted UTF-8 text without involving a document engine."""

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        """Decode text and fence structured formats that are not already Markdown."""
        started_at = perf_counter()
        text = artifact.content.decode("utf-8")
        language = {
            "application/json": "json",
            "application/xml": "xml",
            "text/csv": "csv",
            "text/tab-separated-values": "tsv",
            "text/xml": "xml",
        }.get(artifact.media_type)
        markdown = f"```{language}\n{text.rstrip()}\n```\n" if language else text
        return response(markdown, started_at)


class ImageConverter:
    """Prepare a direct image for the configured visual description enricher."""

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        """Return a minimal derivative that the caption lane enriches with image content."""
        if not artifact.media_type.startswith("image/"):
            raise DoclingConversionError(f"image lane cannot read {artifact.media_type}")
        return response(f"# {artifact.filename}\n", perf_counter())


class OneShotDoclingConverter:
    """Run the heavy layout and OCR lane in a process whose memory dies with the job."""

    def __init__(self, options: DoclingOptions, artifacts_path: Path | None = None) -> None:
        self.options = options
        self.artifacts_path = artifacts_path

    def arguments(self, source: Path, destination: Path) -> tuple[str, ...]:
        """Build the explicit Docling CLI policy for one isolated conversion."""
        arguments = [
            "docling",
            "convert",
            "--to",
            "md",
            "--output",
            str(destination),
            "--image-export-mode",
            self.options.image_export_mode,
            "--pipeline",
            self.options.pipeline,
            "--ocr" if self.options.do_ocr else "--no-ocr",
            "--ocr-engine",
            self.options.ocr_engine,
            "--ocr-lang",
            ",".join(self.options.ocr_languages),
            "--ocr-mode",
            "pdf_aware_layout_regions",
            "--table-mode",
            self.options.table_mode,
            "--document-timeout",
            str(self.options.document_timeout),
            "--device",
            "cpu",
            "--quiet",
        ]
        if self.options.force_ocr:
            arguments.append("--force-ocr")
        if self.artifacts_path is not None:
            arguments.extend(("--artifacts-path", str(self.artifacts_path)))
        for enabled, flag in (
            (self.options.code_enrichment, "--enrich-code"),
            (self.options.formula_enrichment, "--enrich-formula"),
            (self.options.picture_classification, "--enrich-picture-classes"),
            (self.options.picture_description, "--enrich-picture-description"),
            (self.options.chart_extraction, "--enrich-chart-extraction"),
        ):
            if enabled:
                arguments.append(flag)
        arguments.append(str(source))
        return tuple(arguments)

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        """Convert in one subprocess, read its Markdown, then release all model memory."""
        started_at = perf_counter()
        with TemporaryDirectory(prefix="aizk-docling-") as directory:
            root = Path(directory)
            source = root / docling_filename(artifact.filename, artifact.media_type)
            destination = root / "output"
            destination.mkdir()
            source.write_bytes(artifact.content)
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.arguments(source, destination),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as error:
                raise DoclingConversionError(f"Docling could not start because {error}") from error
            try:
                async with asyncio.timeout(self.options.document_timeout + 30):
                    _, errors = await process.communicate()
            except TimeoutError as error:
                process.kill()
                await process.wait()
                raise DoclingConversionError("Docling conversion timed out") from error
            if process.returncode:
                message = errors.decode("utf-8", "replace").strip()
                raise DoclingConversionError(f"Docling conversion failed because {message}")
            markdown_path = destination / f"{source.stem}.md"
            try:
                markdown = markdown_path.read_text()
            except OSError as error:
                listing = json.dumps(sorted(path.name for path in destination.iterdir()))
                raise DoclingConversionError(
                    f"Docling produced no Markdown output and left {listing}"
                ) from error
        return response(markdown, started_at)


class ConversionRouter:
    """Send each artifact to the cheapest lane that can preserve its structure."""

    def __init__(
        self,
        pdf: LiteParseConverter,
        office: PandocConverter,
        text: TextConverter,
        image: ImageConverter,
        heavy: ArtifactConverter,
    ) -> None:
        self.pdf = pdf
        self.office = office
        self.text = text
        self.image = image
        self.heavy = heavy

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        """Route simple PDFs, Office files, text, and complex layouts explicitly."""
        if artifact.media_type == "application/pdf":
            reasons = await self.pdf.inspect(artifact)
            if not reasons:
                logger.info("artifact converter lane=liteparse file={}", artifact.filename)
                return await self.pdf.convert(artifact)
            logger.info(
                "artifact converter lane=docling file={} reasons={}",
                artifact.filename,
                reasons,
            )
            return await self.heavy.convert(artifact)
        if self.office.input_format(artifact.media_type) is not None:
            logger.info("artifact converter lane=pandoc file={}", artifact.filename)
            return await self.office.convert(artifact)
        if artifact.media_type.startswith("text/") or artifact.media_type in {
            "application/json",
            "application/xml",
        }:
            logger.info("artifact converter lane=text file={}", artifact.filename)
            return await self.text.convert(artifact)
        if artifact.media_type.startswith("image/"):
            logger.info("artifact converter lane=caption file={}", artifact.filename)
            return await self.image.convert(artifact)
        logger.info("artifact converter lane=docling file={}", artifact.filename)
        return await self.heavy.convert(artifact)
