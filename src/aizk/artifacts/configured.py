from collections.abc import Hashable
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

import httpx

from ..background.jobs.conversion import ArtifactQueue, DoclingConversionJob, MarkdownReindexJob
from ..config import Settings
from ..integrations.clamav import ClamAVClient, CleanScan, ContentScanner
from ..integrations.converter import (
    ConversionRouter,
    ImageConverter,
    LiteParseConverter,
    OneShotDoclingConverter,
    PandocConverter,
    TextConverter,
)
from ..integrations.docling import (
    ArtifactReader,
    DoclingClient,
    DoclingOptions,
    docling_client,
)
from ..serving.embed import EmbedClient
from ..storage import ByteStore, s3_backend
from .boilerplate import WebBoilerplateCleaner
from .description import ImageDescriptionEnricher, OpenRouterImageCaptioner
from .repository import ArtifactRepository
from .service import ArtifactIntake, ArtifactIntegrity, ArtifactProcessor, ArtifactReindexer
from .visual import DirectImageEnricher


def build_byte_store(config: Settings) -> ByteStore:
    """Build the S3-compatible immutable byte store from explicit settings."""
    backend = s3_backend(
        endpoint=(
            str(config.object_store_endpoint).rstrip("/")
            if not config.object_store_aws_native and config.object_store_endpoint is not None
            else None
        ),
        bucket=config.object_store_bucket,
        access_key=config.object_store_access_key.get_secret_value(),
        secret_key=config.object_store_secret_key.get_secret_value(),
    )
    return ByteStore(
        backend=backend,
        signer=backend,
        upload_byte_limit=config.object_store_upload_byte_limit,
        compression_enabled=config.object_store_compression_enabled,
        compression_level=config.object_store_compression_level,
        compression_min_savings=config.object_store_compression_min_savings,
        internal_download_lifetime=timedelta(
            seconds=config.object_store_internal_download_lifetime_seconds
        ),
    )


@dataclass(frozen=True)
class ArtifactServices:
    """Share one configured intake and conversion job across MCP, web, and the worker."""

    intake: ArtifactIntake
    conversion: DoclingConversionJob
    reindex: MarkdownReindexJob
    integrity: ArtifactIntegrity
    reader: ArtifactReader
    converter: DoclingClient
    scanner: ContentScanner
    http_clients: tuple[httpx.AsyncClient, ...] = ()

    async def aclose(self) -> None:
        """Close every HTTP client the artifact pipeline owns, once at shutdown."""
        for client in self.http_clients:
            await client.aclose()


class TrustedDemoScanner:
    """Accept allowlisted demo files when no private malware scanner is deployed.

    `FormatPolicy` still verifies delivered bytes before this boundary. This mode is
    suitable only for invited demonstration users and is disabled by default.
    """

    async def scan(self, content: bytes) -> CleanScan:
        """Record a trusted-source decision without claiming a malware verdict."""
        return CleanScan(bytes_scanned=len(content))


def build_artifact_services(config: Settings, storage: ByteStore) -> ArtifactServices:
    """Build the artifact pipeline over one byte store from explicit settings."""
    repository = ArtifactRepository(user_byte_limit=config.object_store_user_byte_limit)
    options = DoclingOptions(
        pipeline=config.docling_pipeline,
        image_export_mode="embedded" if config.caption_enabled else "placeholder",
        do_ocr=config.docling_do_ocr,
        force_ocr=config.docling_force_ocr,
        ocr_engine=config.docling_ocr_engine,
        ocr_languages=config.docling_ocr_languages,
        table_mode=config.docling_table_mode,
        code_enrichment=config.docling_code_enrichment,
        formula_enrichment=config.docling_formula_enrichment,
        picture_classification=config.docling_picture_classification,
        chart_extraction=config.docling_chart_extraction,
        picture_description=config.docling_picture_description,
        picture_description_preset=config.docling_picture_description_preset,
        document_timeout=config.docling_document_timeout,
    )
    converter = docling_client(
        str(config.docling_url),
        config.docling_api_key.get_secret_value(),
        config.docling_request_timeout,
        cast(
            "Hashable",
            options,
        ),
    )
    heavy_converter = (
        OneShotDoclingConverter(options, config.docling_artifacts_path)
        if config.docling_artifacts_path is not None
        else converter
    )
    caption_http: httpx.AsyncClient | None = None
    description = None
    if config.caption_enabled:
        caption_http = httpx.AsyncClient(
            base_url=f"{str(config.caption_url).rstrip('/')}/",
            headers={
                "Authorization": (f"Bearer {config.caption_api_key.get_secret_value()}"),
                "X-OpenRouter-Metadata": "enabled",
            },
            timeout=config.caption_request_timeout,
        )
        description = ImageDescriptionEnricher(
            OpenRouterImageCaptioner(
                caption_http,
                config.caption_models,
                config.caption_prompt,
                config.caption_max_tokens,
                config.caption_max_attempts,
                config.caption_backoff_seconds,
            ),
            config.caption_image_byte_limit,
        )
    processor = ArtifactProcessor(
        ConversionRouter(
            LiteParseConverter(),
            PandocConverter(config.docling_document_timeout),
            TextConverter(),
            ImageConverter(),
            heavy_converter,
        ),
        storage,
        repository,
        visual=(
            None
            if config.caption_enabled
            else DirectImageEnricher(EmbedClient.from_settings(config))
        ),
        description=description,
        cleaner=(WebBoilerplateCleaner() if config.artifact_boilerplate_removal_enabled else None),
    )
    conversion = DoclingConversionJob(processor)
    reader = ArtifactReader(
        http=httpx.AsyncClient(timeout=config.artifact_uri_timeout),
        file_root=config.artifact_staging_root,
        max_bytes=config.object_store_upload_byte_limit,
        max_redirects=config.artifact_uri_max_redirects,
    )
    scanner: ContentScanner = (
        ClamAVClient(
            host=config.clamav_host,
            port=config.clamav_port,
            timeout=config.clamav_timeout,
            max_bytes=config.object_store_upload_byte_limit,
        )
        if config.artifact_malware_scan_enabled
        else TrustedDemoScanner()
    )
    return ArtifactServices(
        intake=ArtifactIntake(reader, scanner, storage, repository, ArtifactQueue(conversion)),
        conversion=conversion,
        reindex=MarkdownReindexJob(ArtifactReindexer(repository)),
        integrity=ArtifactIntegrity(storage, repository),
        reader=reader,
        converter=converter,
        scanner=scanner,
        http_clients=tuple(
            client for client in (reader.http, converter.http, caption_http) if client is not None
        ),
    )
