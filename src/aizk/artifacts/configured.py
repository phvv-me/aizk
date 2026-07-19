from collections.abc import Hashable
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

import httpx

from ..background.jobs.conversion import ArtifactQueue, DoclingConversionJob
from ..config import Settings
from ..integrations.clamav import ClamAVClient
from ..integrations.docling import ArtifactReader, DoclingOptions, docling_client
from ..serving.embed import EmbedClient
from ..storage import ByteStore, s3_backend
from .repository import ArtifactRepository
from .service import ArtifactIntake, ArtifactIntegrity, ArtifactProcessor
from .visual import DirectImageEnricher


def build_byte_store(config: Settings) -> ByteStore:
    """Build the S3-compatible immutable byte store from explicit settings."""
    backend = s3_backend(
        endpoint=str(config.object_store_endpoint),
        bucket=config.object_store_bucket,
        access_key=config.object_store_access_key.get_secret_value(),
        secret_key=config.object_store_secret_key.get_secret_value(),
    )
    return ByteStore(
        backend=backend,
        signer=backend,
        upload_byte_limit=config.object_store_upload_byte_limit,
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
    integrity: ArtifactIntegrity
    http_clients: tuple[httpx.AsyncClient, ...] = ()

    async def aclose(self) -> None:
        """Close every HTTP client the artifact pipeline owns, once at shutdown."""
        for client in self.http_clients:
            await client.aclose()


def build_artifact_services(config: Settings, storage: ByteStore) -> ArtifactServices:
    """Build the artifact pipeline over one byte store from explicit settings."""
    repository = ArtifactRepository()
    converter = docling_client(
        str(config.docling_url),
        config.docling_api_key.get_secret_value(),
        config.docling_request_timeout,
        cast(
            "Hashable",
            DoclingOptions(
                pipeline=config.docling_pipeline,
                do_ocr=config.docling_do_ocr,
                force_ocr=config.docling_force_ocr,
                table_mode=config.docling_table_mode,
                code_enrichment=config.docling_code_enrichment,
                formula_enrichment=config.docling_formula_enrichment,
                picture_classification=config.docling_picture_classification,
                chart_extraction=config.docling_chart_extraction,
                picture_description=config.docling_picture_description,
                picture_description_preset=config.docling_picture_description_preset,
                document_timeout=config.docling_document_timeout,
            ),
        ),
    )
    processor = ArtifactProcessor(
        converter,
        storage,
        repository,
        DirectImageEnricher(EmbedClient.from_settings(config)),
    )
    conversion = DoclingConversionJob(processor)
    reader = ArtifactReader(
        http=httpx.AsyncClient(timeout=config.artifact_uri_timeout),
        file_root=config.artifact_staging_root,
        max_bytes=config.object_store_upload_byte_limit,
        max_redirects=config.artifact_uri_max_redirects,
    )
    return ArtifactServices(
        intake=ArtifactIntake(
            reader,
            ClamAVClient(
                host=config.clamav_host,
                port=config.clamav_port,
                timeout=config.clamav_timeout,
                max_bytes=config.object_store_upload_byte_limit,
            ),
            storage,
            repository,
            ArtifactQueue(conversion),
        ),
        conversion=conversion,
        integrity=ArtifactIntegrity(storage, repository),
        http_clients=(reader.http, converter.http),
    )
