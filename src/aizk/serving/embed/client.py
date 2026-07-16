import base64
import io
import mimetypes
from itertools import batched
from pathlib import Path

from openai.types import CreateEmbeddingResponse

from ...config import settings
from ..base import OpenAIService, openai_client, ordered_results
from .models import EmbedImage, EmbedMode


class EmbedClient(OpenAIService):
    """Text and image embeddings through the configured multimodal service."""

    @classmethod
    def configured(cls) -> EmbedClient:
        """Build the service from the live embedding settings."""
        return cls(
            openai_client(
                settings.embed_url,
                settings.embed_api_key,
                settings.embed_request_timeout,
            ),
            settings.embed_model,
        )

    @staticmethod
    def instruction_for(mode: EmbedMode) -> str:
        """Return the configured instruction for a document or query."""
        return (
            settings.embed_instruction_query
            if mode == "query"
            else settings.embed_instruction_document
        )

    @staticmethod
    def instructed(texts: list[str], instruction: str) -> list[str]:
        """Wrap texts in the Qwen embedding instruction scaffold."""
        if not instruction:
            return list(texts)
        return [f"Instruct: {instruction}\nQuery: {text}" for text in texts]

    @staticmethod
    def image_url(image: EmbedImage) -> str:
        """Render an image as a remote URL or local data URI."""
        if isinstance(image, str):
            if image.startswith(("http://", "https://", "data:")):
                return image
            path = Path(image)
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            raw = path.read_bytes()
        else:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            mime, raw = "image/png", buffer.getvalue()
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    async def embed(self, texts: list[str], mode: EmbedMode = "document") -> list[list[float]]:
        """Embed texts in batches and restore the request order."""
        inputs = self.instructed(texts, self.instruction_for(mode))
        vectors: list[list[float]] = []
        for batch in batched(inputs, settings.embed_batch_size, strict=False):
            response = await self.client.embeddings.create(
                model=self.model,
                input=list(batch),
                dimensions=settings.embed_dim,
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
                    "dimensions": settings.embed_dim,
                    "encoding_format": "float",
                    "continue_final_message": True,
                    "add_special_tokens": True,
                },
            )
            vectors.append(response.data[0].embedding)
        return vectors


async def embed(texts: list[str], mode: EmbedMode = "document") -> list[list[float]]:
    """Embed texts through the configured service."""
    return await EmbedClient.configured().embed(texts, mode)


async def embed_images(images: list[EmbedImage]) -> list[list[float]]:
    """Embed images through the configured service."""
    return await EmbedClient.configured().embed_images(images)
