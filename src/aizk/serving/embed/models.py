from typing import BinaryIO, Literal, Protocol

from patos import FrozenModel
from pydantic import field_validator


class WritableImage(Protocol):
    """The one image operation the embedding client needs."""

    def save(self, fp: BinaryIO, format: str) -> None:
        """Serialize this image into a binary stream."""


class ImageBytes(FrozenModel):
    """One in-memory image with its transport media type."""

    content: bytes
    media_type: str

    @field_validator("media_type")
    @classmethod
    def require_image_type(cls, value: str) -> str:
        """Reject non-image payloads at the typed image embedding boundary."""
        if not value.startswith("image/"):
            raise ValueError("image embedding requires an image media type")
        return value


type EmbedMode = Literal["query", "document"]
type EmbedImage = str | ImageBytes | WritableImage
