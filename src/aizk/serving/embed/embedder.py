import base64
import io
import mimetypes
from collections.abc import Mapping
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from openai import AsyncOpenAI, Omit
from openai.types import CreateEmbeddingResponse
from patos import Singleton

from ...config import settings

if TYPE_CHECKING:
    from PIL.Image import Image

type EmbedMode = Literal["query", "document"]


def instruction_for(mode: EmbedMode) -> str:
    """The configured instruction for a mode, document or query.

    mode: whether the caller is about to embed search queries or stored documents.
    """
    return (
        settings.embed_instruction_query
        if mode == "query"
        else settings.embed_instruction_document
    )


def instructed(texts: list[str], instruction: str) -> list[str]:
    """Prefix each text in the Qwen3-Embedding `Instruct: {instruction}\\nQuery: {text}` wrapper.

    The reference Qwen3-Embedding deployment carries no chat template at all, a query is wrapped
    in this literal Instruct/Query prefix and a document is embedded as plain text, so an empty
    instruction, `embed_instruction_document`'s default, leaves every text untouched and only a
    non-empty instruction, always true for a query and opt-in for a document, wraps it.

    texts: input strings about to be embedded.
    instruction: the instruction for this mode, empty to leave every text plain.
    """
    if not instruction:
        return list(texts)
    return [f"Instruct: {instruction}\nQuery: {text}" for text in texts]


def image_url_for(image: str | Image) -> str:
    """Render an image as the `url` an image_url content part carries, a data URI for local bytes.

    A url or an already-formed `data:` URI passes through untouched, while a filesystem path or a
    PIL image is read and base64-encoded into a `data:` URI, so the request never depends on the
    serving box being able to reach the path the client read the image from.

    image: a url, a `data:` URI, a filesystem path, or a PIL image.
    """
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


class Embedder(Singleton):
    """The single embedder, an OpenAI-compatible /v1/embeddings client with a text and image lane.

    A thin HTTP client to the co-resident vLLM container behind `embed_url`. A `patos` singleton,
    built once from `settings` at first construction, so the endpoint, model, and width are fixed
    for the deployment's lifetime. Text batches ride `AsyncEmbeddings.create`. Images ride a
    chat-style `messages` request, the Qwen3-VL-Embedding shape vLLM accepts as a superset of the
    OpenAI embeddings API. Both lanes request `dimensions=embed_dim` and trust the server to
    truncate server-side rather than re-checking every row itself.

    embed_url: base URL of the OpenAI-compatible endpoint, ending at the /v1 prefix.
    embed_model: served model id the endpoint answers to.
    embed_dim: embedding width every request asks the server to truncate to.
    """

    def __init__(self) -> None:
        self.embed_url = settings.embed_url
        self.embed_model = settings.embed_model
        self.embed_dim = settings.embed_dim
        self.api_key = settings.embed_api_key
        # a present key rides as Authorization Bearer; an empty key drops the header entirely via
        # the OpenAI Omit sentinel, which the runtime honors though its stub types headers as str.
        drop_auth: Mapping[str, str] = cast("Mapping[str, str]", {"Authorization": Omit()})
        self.client = AsyncOpenAI(
            base_url=self.embed_url,
            api_key=self.api_key or "none",
            timeout=settings.embed_request_timeout,
            default_headers=None if self.api_key else drop_auth,
        )

    async def embed(self, texts: list[str], mode: EmbedMode = "document") -> list[list[float]]:
        """Embed texts through the OpenAI embeddings client in batches.

        texts: input strings to embed.
        mode: query or document, selecting the instruction wrapped around each input.
        """
        texts = instructed(texts, instruction_for(mode))
        vectors: list[list[float]] = []
        for batch in batched(texts, settings.embed_batch_size, strict=False):
            response = await self.client.embeddings.create(
                model=self.embed_model,
                input=list(batch),
                dimensions=self.embed_dim,
                # pin the float wire format so the SDK never asks for base64, keeping each row a
                # plain list the caller reads directly
                encoding_format="float",
            )
            rows = sorted(response.data, key=lambda row: row.index)
            vectors.extend(row.embedding for row in rows)
        return vectors

    async def embed_images(self, images: list[str | Image]) -> list[list[float]]:
        """Embed images into the shared embed_dim space through the vLLM multimodal path.

        Each image rides as a chat-style `messages` request to the same /v1/embeddings endpoint,
        wrapped in the document instruction so an image vector lands beside the text document
        vectors it answers. The `embeddings.create` helper carries only `input`, never `messages`,
        so the request goes through the client's lower-level `post`, one conversation per image
        since the chat form pools a single vector.

        images: file paths, urls, `data:` URIs, or PIL images to embed.
        """
        instruction = instruction_for("document")
        vectors: list[list[float]] = []
        for image in images:
            response = await self.client.post(
                "/embeddings",
                cast_to=CreateEmbeddingResponse,
                body={
                    "model": self.embed_model,
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
                    "dimensions": self.embed_dim,
                    "encoding_format": "float",
                    "continue_final_message": True,
                    "add_special_tokens": True,
                },
            )
            vectors.append(response.data[0].embedding)
        return vectors
