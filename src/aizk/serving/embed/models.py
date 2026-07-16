from typing import BinaryIO, Literal, Protocol


class WritableImage(Protocol):
    """The one image operation the embedding client needs."""

    def save(self, fp: BinaryIO, format: str) -> None:
        """Serialize this image into a binary stream."""


type EmbedMode = Literal["query", "document"]
type EmbedImage = str | WritableImage
