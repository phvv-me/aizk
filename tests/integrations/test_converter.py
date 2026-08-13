import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from liteparse import LiteParse, PageComplexityStats, ParseError

import aizk.integrations.converter as converter_module
from aizk.integrations.converter import (
    ArtifactConverter,
    ConversionRouter,
    ImageConverter,
    LiteParseConverter,
    OneShotDoclingConverter,
    PandocConverter,
    TextConverter,
)
from aizk.integrations.docling import (
    ArtifactBytes,
    DoclingConversionError,
    DoclingOptions,
    DoclingResponse,
)


class Lane:
    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.calls: list[ArtifactBytes] = []

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        self.calls.append(artifact)
        return DoclingResponse.model_validate(
            {"document": {"md_content": self.markdown}, "status": "success"}
        )


class PdfLane(Lane):
    def __init__(self, markdown: str, reasons: tuple[str, ...]) -> None:
        super().__init__(markdown)
        self.complexity = reasons

    async def inspect(self, artifact: ArtifactBytes) -> tuple[str, ...]:
        del artifact
        return self.complexity


class OfficeLane(Lane):
    def input_format(self, media_type: str) -> str | None:
        return "docx" if media_type.endswith("wordprocessingml.document") else None


class Process:
    def __init__(
        self,
        returncode: int = 0,
        output: bytes = b"",
        errors: bytes = b"",
        timed_out: bool = False,
    ) -> None:
        self.returncode = returncode
        self.output = output
        self.errors = errors
        self.timed_out = timed_out
        self.killed = False

    async def communicate(self, content: bytes | None = None) -> tuple[bytes, bytes]:
        del content
        if self.timed_out:
            raise TimeoutError
        return self.output, self.errors

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def artifact(media_type: str, content: bytes = b"content") -> ArtifactBytes:
    return ArtifactBytes(content=content, filename="document.pdf", media_type=media_type)


def test_router_escalates_complex_pdf_and_keeps_simple_pdf_light() -> None:
    light = PdfLane("light", ())
    office = OfficeLane("office")
    text = TextConverter()
    image = ImageConverter()
    heavy = Lane("heavy")
    router = ConversionRouter(
        cast(LiteParseConverter, light),
        cast(PandocConverter, office),
        text,
        image,
        cast(ArtifactConverter, heavy),
    )

    simple = asyncio.run(router.convert(artifact("application/pdf")))
    light.complexity = ("multi-column",)
    complex_result = asyncio.run(router.convert(artifact("application/pdf")))

    assert simple.markdown == "light\n"
    assert complex_result.markdown == "heavy\n"
    assert len(light.calls) == 1
    assert len(heavy.calls) == 1


def test_router_uses_office_text_and_fallback_lanes() -> None:
    light = PdfLane("light", ())
    office = OfficeLane("office")
    text = Lane("text")
    image = Lane("image")
    heavy = Lane("heavy")
    router = ConversionRouter(
        cast(LiteParseConverter, light),
        cast(PandocConverter, office),
        cast(TextConverter, text),
        cast(ImageConverter, image),
        cast(ArtifactConverter, heavy),
    )

    docx = asyncio.run(
        router.convert(
            artifact("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        )
    )
    plain = asyncio.run(router.convert(artifact("text/plain")))
    image = asyncio.run(router.convert(artifact("image/png")))
    fallback = asyncio.run(router.convert(artifact("application/octet-stream")))

    assert [docx.markdown, plain.markdown, image.markdown] == [
        "office\n",
        "text\n",
        "image\n",
    ]
    assert fallback.markdown == "heavy\n"


def test_image_lane_rejects_non_image_content_and_names_valid_images() -> None:
    converter = ImageConverter()

    with pytest.raises(DoclingConversionError, match="image lane cannot read"):
        asyncio.run(converter.convert(artifact("text/plain")))

    converted = asyncio.run(converter.convert(artifact("image/png")))
    assert converted.markdown == "# document.pdf\n"


def test_pandoc_and_text_lanes_produce_markdown() -> None:
    html = ArtifactBytes(
        content=b"<h1>Title</h1><p>Useful text</p>",
        filename="page.html",
        media_type="text/html",
    )
    structured = ArtifactBytes(
        content=b'{"answer": 42}',
        filename="data.json",
        media_type="application/json",
    )

    converted_html = asyncio.run(PandocConverter(10).convert(html))
    converted_json = asyncio.run(TextConverter().convert(structured))

    assert converted_html.markdown == "# Title\n\nUseful text\n"
    assert converted_json.markdown == '```json\n{"answer": 42}\n```\n'


def test_one_shot_docling_declares_layout_aware_japanese_policy() -> None:
    converter = OneShotDoclingConverter(
        DoclingOptions(ocr_languages=("jpn", "eng", "jpn_vert")),
        Path("/models"),
    )

    arguments = converter.arguments(Path("/tmp/source.pdf"), Path("/tmp/output"))

    assert arguments[0:2] == ("docling", "convert")
    assert "pdf_aware_layout_regions" in arguments
    assert "jpn,eng,jpn_vert" in arguments
    assert arguments[-3:-1] == ("--artifacts-path", "/models")


def test_liteparse_exposes_layout_reasons_and_wraps_parse_errors() -> None:
    class Parser:
        def is_complex(self, content: bytes) -> list[SimpleNamespace]:
            del content
            return [
                SimpleNamespace(
                    reasons=["scanned"],
                    layout=SimpleNamespace(reasons=["multi-column", "scanned"]),
                )
            ]

        def parse(self, content: bytes) -> SimpleNamespace:
            del content
            return SimpleNamespace(text="# Parsed")

    converter = LiteParseConverter()
    converter.parser = cast(LiteParse, Parser())

    reasons = asyncio.run(converter.inspect(artifact("application/pdf")))
    converted = asyncio.run(converter.convert(artifact("application/pdf")))

    assert reasons == ("scanned", "multi-column")
    assert converted.markdown == "# Parsed\n"

    class BrokenParser(Parser):
        def is_complex(self, content: bytes) -> list[SimpleNamespace]:
            del content
            raise ParseError("broken complexity")

        def parse(self, content: bytes) -> SimpleNamespace:
            del content
            raise ParseError("broken parse")

    converter.parser = cast(LiteParse, BrokenParser())
    assert asyncio.run(converter.inspect(artifact("application/pdf"))) == (
        "liteparse-error broken complexity",
    )
    with pytest.raises(DoclingConversionError, match="broken parse"):
        asyncio.run(converter.convert(artifact("application/pdf")))


def test_liteparse_reason_without_layout_stays_empty() -> None:
    page = cast(PageComplexityStats, SimpleNamespace(reasons=[], layout=None))
    assert LiteParseConverter().reasons(page) == ()


def test_pandoc_reports_unsupported_start_failure_timeout_and_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    converter = PandocConverter(1)
    with pytest.raises(DoclingConversionError, match="no route"):
        asyncio.run(converter.convert(artifact("image/png")))

    async def cannot_start(*args: str, **kwargs: int) -> Process:
        del args, kwargs
        raise OSError("missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cannot_start)
    with pytest.raises(DoclingConversionError, match="could not start"):
        asyncio.run(converter.convert(artifact("text/html", b"<p>x</p>")))

    timeout = Process(timed_out=True)

    async def times_out(*args: str, **kwargs: int) -> Process:
        del args, kwargs
        return timeout

    monkeypatch.setattr(asyncio, "create_subprocess_exec", times_out)
    with pytest.raises(DoclingConversionError, match="timed out"):
        asyncio.run(converter.convert(artifact("text/html", b"<p>x</p>")))
    assert timeout.killed

    failed = Process(returncode=2, errors=b"bad input")

    async def exits(*args: str, **kwargs: int) -> Process:
        del args, kwargs
        return failed

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exits)
    with pytest.raises(DoclingConversionError, match="bad input"):
        asyncio.run(converter.convert(artifact("text/html", b"<p>x</p>")))


def test_pandoc_fails_fast_when_binary_wheel_has_no_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = SimpleNamespace(files=())
    monkeypatch.setattr(converter_module, "distribution", lambda name: package)
    converter = PandocConverter.__new__(PandocConverter)
    with pytest.raises(RuntimeError, match="did not contain"):
        converter.resolve_executable()


def test_one_shot_docling_success_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    options = DoclingOptions(
        force_ocr=True,
        code_enrichment=True,
        formula_enrichment=True,
        picture_classification=True,
        picture_description=True,
        chart_extraction=True,
        document_timeout=1,
    )
    converter = OneShotDoclingConverter(options)
    launched: list[tuple[str, ...]] = []

    async def succeeds(*args: str, **kwargs: int) -> Process:
        del kwargs
        launched.append(args)
        destination = Path(args[args.index("--output") + 1])
        source = Path(args[-1])
        (destination / f"{source.stem}.md").write_text("# Heavy")
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", succeeds)
    converted = asyncio.run(converter.convert(artifact("application/pdf", b"%PDF-test")))
    assert converted.markdown == "# Heavy\n"
    assert "--force-ocr" in launched[0]
    assert "--enrich-chart-extraction" in launched[0]

    async def cannot_start(*args: str, **kwargs: int) -> Process:
        del args, kwargs
        raise OSError("missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cannot_start)
    with pytest.raises(DoclingConversionError, match="could not start"):
        asyncio.run(converter.convert(artifact("application/pdf", b"%PDF-test")))

    timeout = Process(timed_out=True)

    async def times_out(*args: str, **kwargs: int) -> Process:
        del args, kwargs
        return timeout

    monkeypatch.setattr(asyncio, "create_subprocess_exec", times_out)
    with pytest.raises(DoclingConversionError, match="timed out"):
        asyncio.run(converter.convert(artifact("application/pdf", b"%PDF-test")))
    assert timeout.killed

    async def exits(*args: str, **kwargs: int) -> Process:
        del args, kwargs
        return Process(returncode=2, errors=b"bad layout")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exits)
    with pytest.raises(DoclingConversionError, match="bad layout"):
        asyncio.run(converter.convert(artifact("application/pdf", b"%PDF-test")))

    async def no_output(*args: str, **kwargs: int) -> Process:
        del args, kwargs
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_output)
    with pytest.raises(DoclingConversionError, match="no Markdown"):
        asyncio.run(converter.convert(artifact("application/pdf", b"%PDF-test")))
