from datetime import UTC, datetime

import dbutil
import pytest
from id_factory import uuid5, uuid7
from patos import sql
from pydantic import UUID7
from sqlmodel import select

from aizk.artifacts import DirectImageEnricher, OriginalArtifact, VisualModality
from aizk.config import settings
from aizk.serving.embed import EmbedImage, ImageBytes
from aizk.store import Artifact, Blob, Chunk, Document
from aizk.store.identity import User
from aizk.store.models.tables import ArtifactContent

pytestmark = pytest.mark.usefixtures("migrated_db")


class Embedder:
    """Record in-memory image boundaries and return one configured-width vector."""

    def __init__(self, vectors: list[list[float]] | None = None) -> None:
        self.images: list[EmbedImage] = []
        self.vectors = [[0.25] * settings.embed_dim] if vectors is None else vectors

    async def embed_images(self, images: list[EmbedImage]) -> list[list[float]]:
        self.images.extend(images)
        return self.vectors


async def seed_image() -> tuple[User, OriginalArtifact, UUID7]:
    """Seed one artifact-backed document with an authoritative text chunk."""
    await dbutil.reset_db()
    owner = uuid5()
    user = User.private(owner)
    original_bytes = b"\x89PNG\r\n\x1a\n preserved"
    async with user as session:
        blob = Blob(
            content_hash=sql.uuid8(original_bytes),
            size=len(original_bytes),
            stored_size=len(original_bytes),
            storage_key="objects/image",
            media_type="image/png",
        )
        artifact = Artifact(
            name="diagram.png",
            created_by=owner,
            scopes=[owner],
        )
        session.add_all((blob, artifact))
        await session.flush()
        content = ArtifactContent(
            artifact_id=artifact.id,
            blob_id=blob.id,
            state=ArtifactContent.State.ready,
            created_by=owner,
            scopes=[owner],
        )
        session.add(content)
        await session.flush()
        document = Document(
            title="Diagram",
            artifact_id=artifact.id,
            artifact_content_id=content.id,
            content_hash=sql.uuid8(b"Docling text"),
            created_by=owner,
            scopes=[owner],
        )
        document.chunks = [
            Chunk(
                document_id=document.id,
                ord=0,
                text="Authoritative extracted structure",
                embedding=[0.5] * settings.embed_dim,
                created_by=owner,
                scopes=[owner],
            )
        ]
        session.add(document)
        await session.flush()
        return (
            user,
            OriginalArtifact(
                artifact_id=artifact.id,
                content_id=content.id,
                revision=1,
                created_by=owner,
                scopes=frozenset({owner}),
                filename=artifact.name,
                media_type="image/png",
                size=len(original_bytes),
                source_uri=None,
                observed_at=datetime(2026, 7, 17, tzinfo=UTC),
                storage_key=blob.storage_key,
                storage_hash=blob.content_hash,
            ),
            document.id,
        )


def test_visual_boundary_is_typed_for_images_and_future_video() -> None:
    enricher = DirectImageEnricher(Embedder())

    assert enricher.modality is VisualModality.image
    assert VisualModality.video.value == "video"
    assert enricher.supports("image/png")
    assert enricher.supports(" IMAGE/PNG; charset=binary")
    assert not enricher.supports("video/mp4")
    assert not enricher.supports("application/pdf")


def test_direct_image_enrichment_keeps_one_supplemental_chunk_on_the_document() -> None:
    async def enrich() -> tuple[list[Chunk], list[EmbedImage], int]:
        user, original, document_id = await seed_image()
        embedder = Embedder()
        enricher = DirectImageEnricher(embedder)
        image = b"\x89PNG\r\n\x1a\n preserved"
        await enricher.enrich(user, document_id, original, image)
        async with user as session:
            visual = (
                await session.exec(
                    select(Chunk).where(
                        Chunk.__table__.c.document_id == document_id,
                        Chunk.__table__.c.ord > 0,
                    )
                )
            ).one()
            visual.provenance = {}
            visual.processed_at = None
        refreshed = original.model_copy(
            update={
                "companion_text": "A system architecture diagram",
                "media_type": "image/png; charset=binary",
            }
        )
        await enricher.enrich(user, document_id, refreshed, image)
        async with user as session:
            chunks = list(
                (
                    await session.exec(
                        select(Chunk)
                        .where(Chunk.__table__.c.document_id == document_id)
                        .order_by(Chunk.__table__.c.ord)
                    )
                ).all()
            )
        outsider = User.private(uuid5())
        async with outsider as session:
            hidden = len(
                (
                    await session.exec(
                        select(Chunk).where(Chunk.__table__.c.document_id == document_id)
                    )
                ).all()
            )
        return chunks, embedder.images, hidden

    chunks, images, hidden = dbutil.run(enrich())

    assert len(chunks) == 2
    assert chunks[0].text == "Authoritative extracted structure"
    visual = chunks[1]
    assert visual.text == "A system architecture diagram"
    assert visual.embedding is not None
    assert visual.processed_at is not None
    assert visual.provenance == {
        "modality": "image",
        "representation": "direct_embedding",
        "role": "supplemental",
        "media_type": "image/png; charset=binary",
        "artifact_content_id": visual.provenance["artifact_content_id"],
    }
    assert "provider" not in visual.provenance
    assert len(images) == 2
    assert all(isinstance(image, ImageBytes) for image in images)
    typed_images = [image for image in images if isinstance(image, ImageBytes)]
    assert all(image.content == b"\x89PNG\r\n\x1a\n preserved" for image in typed_images)
    assert all(image.media_type == "image/png" for image in typed_images)
    assert hidden == 0


def test_direct_image_enrichment_rejects_bad_embedding_cardinality_and_document() -> None:
    async def reject_document() -> None:
        user, original, _ = await seed_image()
        enricher = DirectImageEnricher(Embedder())
        with pytest.raises(LookupError, match="does not match"):
            await enricher.enrich(user, uuid7(), original, b"image")

    original = OriginalArtifact(
        artifact_id=uuid7(),
        content_id=uuid7(),
        revision=1,
        created_by=uuid5(),
        scopes=frozenset(),
        filename="bad.png",
        media_type="image/png",
        size=0,
        source_uri=None,
        storage_key="objects/bad",
        storage_hash=sql.uuid8(b""),
    )
    with pytest.raises(ValueError, match="exactly one vector"):
        dbutil.run(
            DirectImageEnricher(Embedder(vectors=[])).enrich(
                User.system(),
                uuid7(),
                original,
                b"",
            )
        )
    dbutil.run(reject_document())
