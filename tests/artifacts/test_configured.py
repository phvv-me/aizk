import asyncio
from datetime import timedelta
from pathlib import Path
from typing import cast

from obstore.store import MemoryStore

from aizk.artifacts.configured import TrustedDemoScanner, build_artifact_services
from aizk.artifacts.description import ImageDescriptionEnricher
from aizk.artifacts.service import ArtifactProcessor
from aizk.config import Settings
from aizk.integrations.converter import ConversionRouter, OneShotDoclingConverter
from aizk.integrations.docling import DoclingClient
from aizk.storage import ByteStore


def _memory_store() -> ByteStore:
    """Build the bounded in-memory byte store the configured pipeline requires."""
    return ByteStore(
        backend=MemoryStore(),
        upload_byte_limit=1024,
        internal_download_lifetime=timedelta(minutes=5),
    )


def _heavy_converter(artifacts_path: Path | None) -> OneShotDoclingConverter | DoclingClient:
    """Build one pipeline and expose its selected heavy conversion strategy."""
    services = build_artifact_services(
        Settings(docling_artifacts_path=artifacts_path),
        _memory_store(),
    )
    processor = cast(ArtifactProcessor, services.conversion.processor)
    router = cast(ConversionRouter, processor.converter)
    asyncio.run(services.aclose())
    return cast(OneShotDoclingConverter | DoclingClient, router.heavy)


def test_configured_pipeline_isolates_docling_only_when_models_are_local(tmp_path: Path) -> None:
    assert isinstance(_heavy_converter(tmp_path), OneShotDoclingConverter)
    assert isinstance(_heavy_converter(None), DoclingClient)


def test_configured_caption_pipeline_embeds_figures_and_disables_visual_vectors(
    tmp_path: Path,
) -> None:
    services = build_artifact_services(
        Settings(
            caption_enabled=True,
            caption_api_key="demo-key",
            docling_artifacts_path=tmp_path,
        ),
        _memory_store(),
    )
    processor = cast(ArtifactProcessor, services.conversion.processor)
    router = cast(ConversionRouter, processor.converter)

    assert isinstance(router.heavy, OneShotDoclingConverter)
    assert router.heavy.options.image_export_mode == "embedded"
    assert isinstance(processor.description, ImageDescriptionEnricher)
    assert processor.visual is None
    assert len(services.http_clients) == 3
    asyncio.run(services.aclose())


def test_trusted_demo_scanner_records_the_checked_size() -> None:
    verdict = asyncio.run(TrustedDemoScanner().scan(b"trusted demo file"))

    assert verdict.bytes_scanned == 17
