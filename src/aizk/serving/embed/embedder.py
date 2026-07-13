import base64
import io
import mimetypes
from functools import cache
from itertools import batched
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse
from PIL.Image import Image

from ...config import settings

type EmbedMode = Literal["query", "document"]


def instruction_for(mode: EmbedMode) -> str:
    """The configured instruction for a mode, document or query."""
    return (
        settings.embed_instruction_query
        if mode == "query"
        else settings.embed_instruction_document
    )


def instructed(texts: list[str], instruction: str) -> list[str]:
    """Prefix each text in the Qwen3-Embedding `Instruct: {instruction} Query: {text}`
    wrapper."""
    if not instruction:
        return list(texts)
    return [f"Instruct: {instruction}\nQuery: {text}" for text in texts]


def image_url_for(image: str | Image) -> str:
    """Render an image as the `url` an image_url content part carries, a data URI for local
    bytes."""
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


@cache
def _client() -> AsyncOpenAI:
    """Reuse the OpenAI-compatible embedding client for the process lifetime."""
    return AsyncOpenAI(
        base_url=settings.embed_url,
        api_key=settings.embed_api_key or "local",
        timeout=settings.embed_request_timeout,
    )


async def embed(texts: list[str], mode: EmbedMode = "document") -> list[list[float]]:
    """Embed texts through the cached OpenAI-compatible client in batches."""
    inputs = instructed(texts, instruction_for(mode))
    vectors: list[list[float]] = []
    for batch in batched(inputs, settings.embed_batch_size, strict=False):
        response = await _client().embeddings.create(
            model=settings.embed_model,
            input=list(batch),
            dimensions=settings.embed_dim,
            encoding_format="float",
        )
        vectors.extend(row.embedding for row in sorted(response.data, key=lambda row: row.index))
    return vectors


async def embed_images(images: list[str | Image]) -> list[list[float]]:
    """Embed images into the shared vector space through the cached multimodal client."""
    instruction = instruction_for("document")
    vectors: list[list[float]] = []
    for image in images:
        response = await _client().post(
            "/embeddings",
            cast_to=CreateEmbeddingResponse,
            body={
                "model": settings.embed_model,
                "messages": [
                    {"role": "system", "content": [{"type": "text", "text": instruction}]},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_url_for(image)}},
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
