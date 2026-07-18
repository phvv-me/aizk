import base64
import io
import mimetypes
from itertools import batched
from pathlib import Path
from typing import Protocol, runtime_checkable

from openai.types import CreateEmbeddingResponse

from ...config import Settings
from ..base import OpenAIService, openai_client, ordered_results
from .models import EmbedImage, EmbedMode, ImageBytes


@runtime_checkable
class Embedder(Protocol):
    """The text embedding surface recall and the graph builders consume."""

    async def embed(self, texts: list[str], mode: EmbedMode = "document") -> list[list[float]]: ...


class EmbedClient(OpenAIService):
    """Text and image embeddings through the configured multimodal service."""

    dim: int
    batch_size: int
    instruction_query: str
    instruction_document: str

    @classmethod
    def from_settings(cls, config: Settings) -> EmbedClient:
        """Build the service from explicit embedding settings."""
        return cls(
            client=openai_client(
                config.embed_url,
                config.embed_api_key,
                config.embed_request_timeout,
            ),
            model=config.embed_model,
            dim=config.embed_dim,
            batch_size=config.embed_batch_size,
            instruction_query=config.embed_instruction_query,
            instruction_document=config.embed_instruction_document,
        )

    def instruction_for(self, mode: EmbedMode) -> str:
        """Return the configured instruction for a document or query."""
        return self.instruction_query if mode == "query" else self.instruction_document

    @staticmethod
    def instructed(texts: list[str], instruction: str) -> list[str]:
        """Wrap texts in the configured embedding instruction scaffold."""
        if not instruction:
            return list(texts)
        return [f"Instruct: {instruction}\nQuery: {text}" for text in texts]

    @staticmethod
    def image_url(image: EmbedImage) -> str:
        """Render an image as a remote URL or in-memory data URI."""
        if isinstance(image, str):
            if image.startswith(("http://", "https://", "data:")):
                return image
            path = Path(image)
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            raw = path.read_bytes()
        elif isinstance(image, ImageBytes):
            mime, raw = image.media_type, image.content
        else:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            mime, raw = "image/png", buffer.getvalue()
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    async def embed(self, texts: list[str], mode: EmbedMode = "document") -> list[list[float]]:
        """Embed texts in batches and restore the request order."""
        inputs = self.instructed(texts, self.instruction_for(mode))
        vectors: list[list[float]] = []
        for batch in batched(inputs, self.batch_size, strict=False):
            response = await self.client.embeddings.create(
                model=self.model,
                input=list(batch),
                dimensions=self.dim,
                encoding_format="float",
            )
            vectors.extend(
                row.embedding
                for row in ordered_results(
                    response.data,
                    len(batch),
                    "embedder",
                    lambda result: result.index,
                )
            )
        return vectors

    async def embed_images(self, images: list[EmbedImage]) -> list[list[float]]:
        """Embed images into the shared vector space."""
        instruction = self.instruction_for("document")
        vectors: list[list[float]] = []
        for image in images:
            response = await self.client.post(
                "/embeddings",
                cast_to=CreateEmbeddingResponse,
                body={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": [{"type": "text", "text": instruction}]},
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": self.image_url(image)}},
                                {"type": "text", "text": ""},
                            ],
                        },
                        {"role": "assistant", "content": [{"type": "text", "text": ""}]},
                    ],
                    "dimensions": self.dim,
                    "encoding_format": "float",
                    "continue_final_message": True,
                    "add_special_tokens": True,
                },
            )
            vectors.append(response.data[0].embedding)
        return vectors
