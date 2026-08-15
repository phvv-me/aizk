import asyncio
import base64
import binascii
import hashlib
import re
from time import perf_counter
from typing import Protocol, cast

import httpx
from loguru import logger
from patos import FrozenModel, FrozenOpenModel
from pydantic import Field, JsonValue

from .models import OriginalArtifact

_EMBEDDED_IMAGE = re.compile(
    r"!\[(?P<alt>[^\]\n]*)\]\(\s*"
    r"data:(?P<media_type>image/[a-zA-Z0-9.+-]+);base64,"
    r"(?P<data>[a-zA-Z0-9+/=\s]+)\s*\)",
)
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429})


class CaptionError(RuntimeError):
    """No configured caption route could describe an image safely."""


class CaptionAttempt(FrozenModel):
    """One bounded provider attempt made while resolving an image description."""

    requested_model: str
    attempt: int
    elapsed_ms: float
    status_code: int | None = None
    error: str | None = None


class CaptionUsage(FrozenModel):
    """Provider-reported token and dollar accounting for one completed caption."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0


class ImageCaption(FrozenModel):
    """One factual image description with its exact routing provenance."""

    text: str
    requested_model: str
    model: str
    provider: str | None = None
    elapsed_ms: float
    usage: CaptionUsage = Field(default_factory=CaptionUsage)
    attempts: tuple[CaptionAttempt, ...]


class FigureDescription(FrozenModel):
    """Tie one stored caption to the exact image bytes it describes."""

    ordinal: int
    image_sha256: str
    media_type: str
    alt_text: str | None = None
    caption: ImageCaption


class DescribedArtifact(FrozenModel):
    """Converted Markdown after embedded figures have become searchable prose."""

    markdown: str
    figures: tuple[FigureDescription, ...] = ()

    def metadata(self) -> list[dict[str, JsonValue]]:
        """Serialize routing provenance for durable artifact storage."""
        return [
            cast(dict[str, JsonValue], figure.model_dump(mode="json")) for figure in self.figures
        ]


class ImageCaptioner(Protocol):
    """Describe one in-memory image through a bounded provider chain."""

    async def caption(self, content: bytes, media_type: str, context: str) -> ImageCaption:
        """Return factual searchable prose and exact provider metadata."""
        ...


class ArtifactDescriptionEnricher(Protocol):
    """Turn visual artifact content into text before ordinary text embedding."""

    async def enrich(
        self,
        original: OriginalArtifact,
        content: bytes,
        markdown: str,
    ) -> DescribedArtifact:
        """Return Markdown with image data replaced by descriptions."""
        ...


class _OpenRouterMessage(FrozenOpenModel):
    content: str | None = None


class _OpenRouterChoice(FrozenOpenModel):
    message: _OpenRouterMessage


class _OpenRouterUsage(FrozenOpenModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0


class _OpenRouterResponse(FrozenOpenModel):
    model: str
    provider: str | None = None
    choices: list[_OpenRouterChoice] = Field(min_length=1)
    usage: _OpenRouterUsage = Field(default_factory=_OpenRouterUsage)


class OpenRouterImageCaptioner:
    """Call one named vision model with retries, then explicit named fallbacks."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        models: tuple[str, ...],
        prompt: str,
        max_tokens: int,
        attempts: int,
        backoff_seconds: float,
    ) -> None:
        if not models:
            raise ValueError("image captioning requires at least one model")
        if attempts < 1:
            raise ValueError("image caption attempts must be positive")
        self.http = http
        self.models = tuple(dict.fromkeys(models))
        self.prompt = prompt
        self.max_tokens = max_tokens
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds

    async def caption(self, content: bytes, media_type: str, context: str) -> ImageCaption:
        """Describe one image, retrying transient failures before changing model."""
        attempts: list[CaptionAttempt] = []
        encoded = base64.b64encode(content).decode("ascii")
        text = self.prompt if not context else f"{self.prompt}\n\nImage context is {context}."
        payload_content: list[dict[str, JsonValue]] = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}"},
            },
            {"type": "text", "text": text},
        ]
        for model in self.models:
            if caption := await self._caption_with_model(model, payload_content, attempts):
                return caption
        summary = ", ".join(
            f"{attempt.requested_model} HTTP {attempt.status_code or 'network'}"
            for attempt in attempts
        )
        raise CaptionError(f"every image caption route failed after {summary}")

    async def _caption_with_model(
        self,
        model: str,
        payload_content: list[dict[str, JsonValue]],
        attempts: list[CaptionAttempt],
    ) -> ImageCaption | None:
        """Try one model route and return its caption or allow the next route."""
        for attempt in range(1, self.attempts + 1):
            started_at = perf_counter()
            try:
                response = await self.http.post(
                    "chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": payload_content}],
                        "max_tokens": self.max_tokens,
                        "temperature": 0,
                        "provider": {"data_collection": "allow"},
                    },
                )
            except httpx.RequestError as request_error:
                attempts.append(
                    CaptionAttempt(
                        requested_model=model,
                        attempt=attempt,
                        elapsed_ms=(perf_counter() - started_at) * 1000,
                        error=type(request_error).__name__,
                    )
                )
                if attempt < self.attempts:
                    await self.backoff(attempt)
                continue
            elapsed_ms = (perf_counter() - started_at) * 1000
            if response.is_success:
                caption = await self._successful_caption(
                    response,
                    model,
                    attempt,
                    elapsed_ms,
                    attempts,
                )
                if caption is not None:
                    return caption
                continue
            provider_error = self.error_message(response)
            attempts.append(
                CaptionAttempt(
                    requested_model=model,
                    attempt=attempt,
                    elapsed_ms=elapsed_ms,
                    status_code=response.status_code,
                    error=provider_error,
                )
            )
            logger.warning(
                "image caption model={} attempt={} status={} error={}",
                model,
                attempt,
                response.status_code,
                provider_error,
            )
            retryable = response.status_code in _RETRYABLE_STATUS or response.status_code >= 500
            if retryable and attempt < self.attempts:
                await self.backoff(attempt)
                continue
            if response.status_code == 404 or retryable:
                return None
            raise CaptionError(
                "image caption request failed with HTTP"
                f" {response.status_code} because {provider_error}"
            )
        return None

    async def _successful_caption(
        self,
        response: httpx.Response,
        model: str,
        attempt: int,
        elapsed_ms: float,
        attempts: list[CaptionAttempt],
    ) -> ImageCaption | None:
        """Validate one successful response and record malformed answers for retry."""
        try:
            parsed = _OpenRouterResponse.model_validate(response.json())
            caption = parsed.choices[0].message.content
        except ValueError as parse_error:
            attempts.append(
                CaptionAttempt(
                    requested_model=model,
                    attempt=attempt,
                    elapsed_ms=elapsed_ms,
                    status_code=response.status_code,
                    error=str(parse_error)[:256],
                )
            )
            if attempt < self.attempts:
                await self.backoff(attempt)
            return None
        if caption is None or not caption.strip():
            attempts.append(
                CaptionAttempt(
                    requested_model=model,
                    attempt=attempt,
                    elapsed_ms=elapsed_ms,
                    status_code=response.status_code,
                    error="provider returned an empty caption",
                )
            )
            if attempt < self.attempts:
                await self.backoff(attempt)
            return None
        attempts.append(
            CaptionAttempt(
                requested_model=model,
                attempt=attempt,
                elapsed_ms=elapsed_ms,
                status_code=response.status_code,
            )
        )
        return ImageCaption(
            text=caption.strip(),
            requested_model=model,
            model=parsed.model,
            provider=parsed.provider,
            elapsed_ms=elapsed_ms,
            usage=CaptionUsage.model_validate(parsed.usage.model_dump()),
            attempts=tuple(attempts),
        )

    async def backoff(self, attempt: int) -> None:
        """Pause by one bounded exponential interval before a same-model retry."""
        await asyncio.sleep(self.backoff_seconds * 2 ** (attempt - 1))

    @staticmethod
    def error_message(response: httpx.Response) -> str:
        """Read a short provider error without ever echoing image request data."""
        try:
            parsed = response.json()
        except ValueError:
            return response.text[:256] or "provider returned no error body"
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str):
                    return message[:256]
        return "provider returned an unrecognized error body"


class ImageDescriptionEnricher:
    """Replace embedded document figures and direct images with factual text."""

    def __init__(self, captioner: ImageCaptioner, image_byte_limit: int) -> None:
        self.captioner = captioner
        self.image_byte_limit = image_byte_limit

    async def enrich(
        self,
        original: OriginalArtifact,
        content: bytes,
        markdown: str,
    ) -> DescribedArtifact:
        """Describe every embedded figure, or the original when it is itself an image."""
        matches = tuple(_EMBEDDED_IMAGE.finditer(markdown))
        if matches:
            return await self.embedded(markdown, matches)
        if "data:image/" in markdown.casefold():
            raise CaptionError("converted Markdown contains an unsupported embedded image")
        media_type = original.media_type.partition(";")[0].strip().casefold()
        if not media_type.startswith("image/"):
            return DescribedArtifact(markdown=markdown)
        caption = await self.describe(
            content,
            media_type,
            original.companion_text or original.filename,
        )
        figure = self.figure(0, content, media_type, original.companion_text, caption)
        return DescribedArtifact(
            markdown=f"{markdown.rstrip()}\n\n## Visual description\n\n{caption.text}\n",
            figures=(figure,),
        )

    async def embedded(
        self,
        markdown: str,
        matches: tuple[re.Match[str], ...],
    ) -> DescribedArtifact:
        """Caption unique embedded bytes once and replace each data URI in source order."""
        descriptions: list[FigureDescription] = []
        replacements: list[str] = []
        cached: dict[tuple[str, bytes], ImageCaption] = {}
        for ordinal, match in enumerate(matches):
            media_type = match.group("media_type").casefold()
            try:
                content = base64.b64decode(
                    "".join(match.group("data").split()),
                    validate=True,
                )
            except binascii.Error as error:
                raise CaptionError("embedded figure contains invalid base64 data") from error
            key = (media_type, content)
            caption = cached.get(key)
            if caption is None:
                caption = await self.describe(content, media_type, match.group("alt"))
                cached[key] = caption
            descriptions.append(
                self.figure(ordinal, content, media_type, match.group("alt"), caption)
            )
            label = (
                f"Figure description for {match.group('alt').strip()}"
                if match.group("alt").strip()
                else "Figure description"
            )
            replacements.append(f"\n\n**{label}**\n\n{caption.text}\n\n")
        pieces: list[str] = []
        cursor = 0
        for match, replacement in zip(matches, replacements, strict=True):
            pieces.extend((markdown[cursor : match.start()], replacement))
            cursor = match.end()
        pieces.append(markdown[cursor:])
        enriched = "".join(pieces)
        if "data:image/" in enriched.casefold():
            raise CaptionError("converted Markdown contains an unsupported embedded image")
        return DescribedArtifact(markdown=enriched, figures=tuple(descriptions))

    async def describe(self, content: bytes, media_type: str, context: str) -> ImageCaption:
        """Enforce the byte boundary before one image leaves the deployment."""
        if not content:
            raise CaptionError("cannot caption an empty image")
        if len(content) > self.image_byte_limit:
            raise CaptionError("image exceeds the configured caption byte limit")
        return await self.captioner.caption(content, media_type, context)

    @staticmethod
    def figure(
        ordinal: int,
        content: bytes,
        media_type: str,
        alt_text: str | None,
        caption: ImageCaption,
    ) -> FigureDescription:
        """Build stable per-image metadata without retaining the original bytes."""
        return FigureDescription(
            ordinal=ordinal,
            image_sha256=hashlib.sha256(content).hexdigest(),
            media_type=media_type,
            alt_text=alt_text.strip() if alt_text and alt_text.strip() else None,
            caption=caption,
        )
