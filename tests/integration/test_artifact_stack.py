import asyncio
import base64
from collections.abc import Iterator
from contextlib import suppress

import dbutil
import mcp_probe
import pytest
from fastmcp.resources import ResourceContent, ResourceResult
from mcp_probe import context_for
from obstore.store import S3Store
from pydantic import UUID7
from sqlmodel import select

from aizk.artifacts import ArtifactReceipt
from aizk.background.schedule import run_worker
from aizk.config import settings
from aizk.integrations.clamav import MalwareRejectedError
from aizk.integrations.docling import ArtifactBytes
from aizk.memory import Memory
from aizk.store import Artifact, Blob, Chunk, Document
from aizk.store.identity import User
from aizk.store.identity.organization import OrganizationStanding
from aizk.store.models.tables import ArtifactContent
from aizk.types import ScopeNames

pytestmark = [
    pytest.mark.integration,
    pytest.mark.artifact_stack,
    pytest.mark.usefixtures("migrated_db"),
]

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGOUr7/PwMDAxMDAwMDAAAARRgGBumiF5gAAAABJRU5ErkJggg=="
)
_EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def tiny_pdf(text: str) -> bytes:
    """Build one valid single-page PDF with searchable Helvetica text."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    )
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, value in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode())
        body.extend(value)
        body.extend(b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(body)


@pytest.fixture(scope="module", autouse=True)
def real_worker(migrated_db: None) -> Iterator[None]:
    """Run the actual PgQueuer worker and remove test objects after the isolated database."""

    async def start() -> asyncio.Task[None]:
        task = asyncio.create_task(run_worker(mcp_probe.runtime, batch_size=8))
        await asyncio.sleep(0)
        return task

    task = dbutil.run(start())
    yield

    async def stop_and_clean() -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        async with User.system().owner as session:
            blobs = list(await session.exec(select(Blob)))
        for blob in blobs:
            await mcp_probe.runtime.store.delete(blob.storage_key)

    dbutil.run(stop_and_clean())


async def accept(
    user: User,
    content: bytes,
    filename: str,
    media_type: str,
    source_uri: str | None = None,
    companion_text: str | None = None,
    scopes: ScopeNames | None = None,
) -> ArtifactReceipt:
    """Accept one real artifact and wait for its PgQueuer conversion outcome."""
    async with asyncio.timeout(600):
        receipt = await mcp_probe.runtime.artifacts.intake.accept(
            user,
            ArtifactBytes(content=content, filename=filename, media_type=media_type),
            source_uri=source_uri,
            companion_text=companion_text,
            scopes=scopes,
        )
        await terminal_content(user, receipt)
        return receipt


async def terminal_content(user: User, receipt: ArtifactReceipt) -> ArtifactContent:
    """Wait until the real worker records a durable conversion outcome."""
    async with asyncio.timeout(600):
        while True:
            async with user as session:
                content = await session.get(Artifact.Content, receipt.content_id)
            assert content is not None
            if content.state in (Artifact.Content.State.ready, Artifact.Content.State.failed):
                return content
            await asyncio.sleep(0.25)


async def artifact_document(user: User, receipt: ArtifactReceipt) -> tuple[Document, list[Chunk]]:
    """Load the logical document and every text or visual chunk for one exact revision."""
    async with asyncio.timeout(600):
        while True:
            async with user as session:
                document = (
                    await session.exec(
                        select(Document).where(
                            Document.__table__.c.artifact_id == receipt.artifact_id,
                            Document.__table__.c.artifact_content_id == receipt.content_id,
                        )
                    )
                ).first()
                chunks = (
                    list(
                        await session.exec(
                            select(Chunk)
                            .where(Chunk.__table__.c.document_id == document.id)
                            .order_by(Chunk.__table__.c.ord)
                        )
                    )
                    if document is not None
                    else []
                )
            if document is not None and chunks:
                return document, chunks
            await asyncio.sleep(0.25)


async def wait_for_projection(user: User, document_id: UUID7) -> list[Chunk]:
    """Wait for every ordinary source chunk to pass the real extraction worker."""
    async with asyncio.timeout(600):
        while True:
            async with user as session:
                stored = list(
                    await session.exec(
                        select(Chunk).where(Chunk.__table__.c.document_id == document_id)
                    )
                )
            chunks = [chunk for chunk in stored if "modality" not in chunk.provenance]
            if chunks and all(chunk.processed_at is not None for chunk in chunks):
                return chunks
            await asyncio.sleep(0.25)


async def object_count() -> int:
    """Count stored immutable objects through the real SeaweedFS S3 interface."""
    store = S3Store(
        settings.object_store_bucket,
        endpoint=str(settings.object_store_endpoint),
        access_key_id=settings.object_store_access_key.get_secret_value(),
        secret_access_key=settings.object_store_secret_key.get_secret_value(),
        client_options={"allow_http": True},
    )
    return sum([len(batch) async for batch in store.list_async(prefix="objects/", chunk_size=100)])


@pytest.mark.parametrize(
    ("filename", "media_type", "body", "sentinel"),
    [
        (
            "artifact-integration-note.md",
            "text/markdown",
            b"# Integration note\n\nThe cobalt heron validates the real text artifact path.\n",
            "cobalt heron",
        ),
        (
            "artifact-integration-paper.pdf",
            "application/pdf",
            tiny_pdf("The amber kestrel validates the real PDF artifact path."),
            "amber kestrel",
        ),
    ],
    ids=["text", "pdf"],
)
def test_text_and_pdf_cross_every_real_conversion_and_projection_boundary(
    filename: str,
    media_type: str,
    body: bytes,
    sentinel: str,
) -> None:
    user = User.private(settings.default_user_id)

    async def verify() -> None:
        receipt = await accept(user, body, filename, media_type)
        content = await terminal_content(user, receipt)
        assert content.state is Artifact.Content.State.ready
        assert content.markdown is not None
        assert sentinel in content.markdown.casefold()
        document, chunks = await artifact_document(user, receipt)
        assert all(chunk.embedding is not None for chunk in chunks)
        projected = await wait_for_projection(user, document.id)
        assert all(chunk.processed_at is not None for chunk in projected)

    dbutil.run(verify())


def test_png_keeps_a_direct_multimodal_embedding_on_its_converted_document() -> None:
    user = User.private(settings.default_user_id)

    async def verify() -> None:
        receipt = await accept(
            user,
            _PNG,
            "artifact-integration-pixel.png",
            "image/png",
            companion_text="A one-pixel visual integration fixture.",
        )
        content = await terminal_content(user, receipt)
        assert content.state is Artifact.Content.State.ready
        document, chunks = await artifact_document(user, receipt)
        [visual] = [
            chunk
            for chunk in chunks
            if chunk.provenance.get("modality") == "image"
            and chunk.provenance.get("representation") == "direct_embedding"
        ]
        assert visual.document_id == document.id
        assert visual.embedding is not None
        assert visual.processed_at is not None

    dbutil.run(verify())


def test_unsupported_binary_becomes_recallable_metadata_without_false_content() -> None:
    user = User.private(settings.default_user_id)

    async def verify() -> None:
        receipt = await accept(
            user,
            b"\x00\xff\x01\xfeunsupported-aizk-binary\x00",
            "artifact-integration-unsupported.aizkbin",
            "application/octet-stream",
        )
        content = await terminal_content(user, receipt)
        assert content.state is Artifact.Content.State.failed
        assert not content.markdown
        _, chunks = await artifact_document(user, receipt)
        rendered = "\n".join(chunk.text for chunk in chunks)
        assert "artifact-integration-unsupported.aizkbin" in rendered
        assert "Media type application/octet-stream" in rendered
        assert "Conversion state failed" in rendered
        assert all(chunk.embedding is not None for chunk in chunks)
        result = await Memory(user=user, intake=mcp_probe.runtime.artifacts.intake).recall(
            "What is artifact-integration-unsupported.aizkbin?",
            4000,
        )
        assert any(
            "artifact-integration-unsupported.aizkbin" in evidence.text
            for evidence in result.evidence
        )

    dbutil.run(verify())


def test_eicar_is_rejected_by_real_clamav_before_object_or_metadata_storage() -> None:
    user = User.private(settings.default_user_id)

    async def verify() -> None:
        before_objects = await object_count()
        async with user as session:
            before_blobs = (await session.exec(select(Blob.id.count()))).one()
            before_artifacts = (await session.exec(select(Artifact.id.count()))).one()
        with pytest.raises(MalwareRejectedError):
            await mcp_probe.runtime.artifacts.intake.accept(
                user,
                ArtifactBytes(
                    content=_EICAR,
                    filename="eicar.com.txt",
                    media_type="text/plain",
                ),
            )
        async with user as session:
            assert (await session.exec(select(Blob.id.count()))).one() == before_blobs
            assert (await session.exec(select(Artifact.id.count()))).one() == before_artifacts
        assert await object_count() == before_objects

    dbutil.run(verify())


def test_changed_source_bytes_create_a_revision_and_refresh_the_stable_document() -> None:
    user = User.private(settings.default_user_id)
    uri = "https://example.org/aizk-artifact-integration-revision.md"

    async def verify() -> None:
        first = await accept(
            user,
            b"# Revision fixture\n\nThe standing revision is indigo.\n",
            "artifact-integration-revision.md",
            "text/markdown",
            source_uri=uri,
        )
        first_document, _ = await artifact_document(user, first)
        second = await accept(
            user,
            b"# Revision fixture\n\nThe standing revision is vermilion.\n",
            "artifact-integration-revision.md",
            "text/markdown",
            source_uri=uri,
        )
        second_document, chunks = await artifact_document(user, second)
        async with user as session:
            revisions = list(
                await session.exec(
                    select(Artifact.Content)
                    .where(Artifact.Content.__table__.c.artifact_id == first.artifact_id)
                    .order_by(Artifact.Content.__table__.c.revision)
                )
            )
        assert first.artifact_id == second.artifact_id
        assert first.content_id != second.content_id
        assert [revision.revision for revision in revisions] == [1, 2]
        assert first_document.id == second_document.id
        assert second_document.artifact_content_id == second.content_id
        rendered = "\n".join(chunk.text for chunk in chunks)
        assert "vermilion" in rendered
        assert "indigo" not in rendered

    dbutil.run(verify())


def test_recall_exposes_exact_provenance_and_resource_returns_original_bytes() -> None:
    organization_id = settings.scope_id("artifact-integration-organization")
    organization = OrganizationStanding(
        id=organization_id,
        name="Artifact Integration",
        description="Real artifact stack validation",
        roles=("editor",),
        permissions=(settings.logto_write_permission,),
    )
    user = User.authorized(
        settings.default_user_id,
        read=(settings.default_user_id, organization_id),
        write=(settings.default_user_id, organization_id),
        organizations=(organization,),
    )
    body = (
        b"# Provenance fixture\n\n"
        b"The silver osprey proves recall and resource provenance round-trip.\n"
    )
    context = context_for(user)

    async def verify() -> None:
        receipt = await accept(
            user,
            body,
            "artifact-integration-provenance.md",
            "text/markdown",
            scopes=["Artifact Integration"],
        )
        result = await Memory(user=user, intake=mcp_probe.runtime.artifacts.intake).recall(
            "What does artifact-integration-provenance.md say about the silver osprey?",
            4000,
        )
        resource_uri = f"aizk://artifacts/{receipt.artifact_id}/contents/{receipt.content_id}"
        evidence = next(
            evidence
            for evidence in result.evidence
            if "silver osprey" in evidence.text.casefold()
            and evidence.resource_uri == resource_uri
        )
        assert evidence.scopes == (
            result.Scope(
                name="Artifact Integration",
                description="Real artifact stack validation",
            ),
        )
        resource = await mcp_probe.server.artifact_resource()(
            receipt.artifact_id,
            receipt.content_id,
            context,
        )
        assert resource == ResourceResult(
            contents=[ResourceContent(body, mime_type="text/markdown")]
        )

    dbutil.run(verify())
