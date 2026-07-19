from datetime import UTC, datetime
from enum import StrEnum, auto
from typing import Protocol

from pydantic import UUID7, JsonValue

from ..serving.embed import EmbedImage, ImageBytes
from ..store import Chunk, Document
from ..store.identity import User
from .models import OriginalArtifact

_VISUAL_ORDINAL = 2_147_483_647


class VisualModality(StrEnum):
    """Typed visual enrichment kinds supported by independent implementations."""

    image = auto()
    video = auto()


class ImageEmbedder(Protocol):
    """The generic multimodal embedding operation image enrichment requires."""

    async def embed_images(self, images: list[EmbedImage]) -> list[list[float]]:
        """Embed ordered images into the shared retrieval space."""
        ...


class ArtifactVisualEnricher(Protocol):
    """One modality-specific extension after authoritative structural conversion."""

    modality: VisualModality

    def supports(self, media_type: str) -> bool:
        """Return whether this implementation accepts the artifact media type."""
        ...

    async def enrich(
        self,
        user: User,
        document_id: UUID7,
        original: OriginalArtifact,
        content: bytes,
    ) -> None:
        """Store supplemental visual retrieval data on the converted document."""
        ...


class DirectImageEnricher:
    """Attach one direct in-memory image embedding to the converted logical document."""

    modality = VisualModality.image

    def __init__(self, embedder: ImageEmbedder) -> None:
        self.embedder = embedder

    def supports(self, media_type: str) -> bool:
        """Accept image artifacts while leaving video to a separate future implementation."""
        normalized = media_type.partition(";")[0].strip().casefold()
        return normalized.startswith(f"{self.modality}/")

    async def enrich(
        self,
        user: User,
        document_id: UUID7,
        original: OriginalArtifact,
        content: bytes,
    ) -> None:
        """Upsert one direct image vector without changing Docling-derived text chunks."""
        vectors = await self.embedder.embed_images(
            [
                ImageBytes(
                    content=content,
                    media_type=original.media_type.partition(";")[0].strip().casefold(),
                )
            ]
        )
        if len(vectors) != 1:
            raise ValueError("image embedder did not return exactly one vector")
        provenance: dict[str, JsonValue] = {
            "modality": self.modality.value,
            "representation": "direct_embedding",
            "role": "supplemental",
            "media_type": original.media_type,
            "artifact_content_id": str(original.content_id),
        }
        async with user as session:
            document = await session.get(Document, document_id)
            if (
                document is None
                or document.artifact_id != original.artifact_id
                or document.artifact_content_id != original.content_id
            ):
                raise LookupError("visual document does not match its artifact revision")
            chunk = (await session.exec(Chunk.at(document_id, _VISUAL_ORDINAL))).first()
            text = original.companion_text or f"Visual content from {original.filename}"
            now = datetime.now(UTC)
            if chunk is None:
                session.add(
                    Chunk(
                        document_id=document_id,
                        ord=_VISUAL_ORDINAL,
                        text=text,
                        embedding=vectors[0],
                        provenance=provenance,
                        processed_at=now,
                        created_by=original.created_by,
                        scopes=sorted(original.scopes, key=str),
                    )
                )
                return
            chunk.text = text
            chunk.embedding = vectors[0]
            chunk.provenance = provenance
            chunk.processed_at = now
            chunk.created_by = original.created_by
            chunk.scopes = sorted(original.scopes, key=str)
