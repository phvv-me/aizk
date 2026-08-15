import asyncio
import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from id_factory import uuid5, uuid7
from patos import sql

from aizk.artifacts.description import (
    CaptionAttempt,
    CaptionError,
    CaptionUsage,
    ImageCaption,
    ImageDescriptionEnricher,
    OpenRouterImageCaptioner,
)
from aizk.artifacts.models import OriginalArtifact


def original(media_type: str = "application/pdf") -> OriginalArtifact:
    content = b"original"
    owner = uuid5()
    return OriginalArtifact(
        artifact_id=uuid7(),
        content_id=uuid7(),
        revision=1,
        created_by=owner,
        scopes=frozenset({owner}),
        filename="paper.pdf",
        media_type=media_type,
        size=len(content),
        source_uri="https://files.example/paper.pdf",
        observed_at=datetime(2026, 8, 10, tzinfo=UTC),
        storage_key="objects/original",
        storage_hash=sql.uuid8(content),
    )


def completion(
    model: str = "primary",
    provider: str = "Provider",
    caption: str | None = "A precise scientific chart.",
) -> dict:
    return {
        "model": model,
        "provider": provider,
        "choices": [{"message": {"content": caption}}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cost": 0.001,
        },
    }


async def request_caption(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    models: tuple[str, ...] = ("primary", "fallback"),
    attempts: int = 2,
) -> ImageCaption:
    async with httpx.AsyncClient(
        base_url="https://openrouter.test/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as http:
        return await OpenRouterImageCaptioner(
            http,
            models,
            "Describe the figure.",
            max_tokens=100,
            attempts=attempts,
            backoff_seconds=0,
        ).caption(b"image", "image/png", "chart")


def test_openrouter_caption_success_keeps_exact_routing_and_usage() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=completion(model="actual", provider="CoreWeave"))

    caption = asyncio.run(request_caption(handler))

    assert caption.text == "A precise scientific chart."
    assert caption.requested_model == "primary"
    assert caption.model == "actual"
    assert caption.provider == "CoreWeave"
    assert caption.usage == CaptionUsage(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost=0.001,
    )
    assert caption.attempts[0].status_code == 200
    payload = json.loads(requests[0].content)
    assert payload["provider"] == {"data_collection": "allow"}
    assert payload["messages"][0]["content"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert payload["messages"][0]["content"][1]["text"].endswith("Image context is chart.")


def test_openrouter_caption_retries_transient_errors_then_uses_fallback() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        requested.append(model)
        if model == "primary":
            return httpx.Response(429, json={"error": {"message": "shared pool busy"}})
        return httpx.Response(200, json=completion(model="fallback", provider="Nvidia"))

    caption = asyncio.run(request_caption(handler))

    assert requested == ["primary", "primary", "fallback"]
    assert caption.requested_model == "fallback"
    assert [attempt.status_code for attempt in caption.attempts] == [429, 429, 200]
    assert caption.attempts[0].error == "shared pool busy"


def test_openrouter_caption_falls_back_after_a_missing_route() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        requested.append(model)
        if model == "primary":
            return httpx.Response(404, text="no route")
        return httpx.Response(200, json=completion(model="fallback"))

    caption = asyncio.run(request_caption(handler))

    assert requested == ["primary", "fallback"]
    assert caption.attempts[0].error == "no route"


def test_openrouter_caption_retries_network_and_malformed_successes() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline", request=request)
        if calls == 2:
            return httpx.Response(200, json=completion(caption=None))
        return httpx.Response(200, json=completion(model="fallback"))

    caption = asyncio.run(request_caption(handler, attempts=2))

    assert caption.requested_model == "fallback"
    assert [attempt.error for attempt in caption.attempts[:2]] == [
        "ConnectError",
        "provider returned an empty caption",
    ]


def test_openrouter_caption_retries_an_empty_success_on_the_same_model() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        return httpx.Response(200, json=completion(caption=None if calls == 1 else "Ready"))

    caption = asyncio.run(request_caption(handler, models=("primary",), attempts=2))

    assert caption.text == "Ready"
    assert [attempt.status_code for attempt in caption.attempts] == [200, 200]


def test_openrouter_caption_retries_a_malformed_success_on_the_same_model() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        body = {"unexpected": True} if calls == 1 else completion(caption="Ready")
        return httpx.Response(200, json=body)

    caption = asyncio.run(request_caption(handler, models=("primary",), attempts=2))

    assert caption.text == "Ready"
    assert [attempt.status_code for attempt in caption.attempts] == [200, 200]
    assert caption.attempts[0].error is not None


def test_openrouter_caption_falls_back_after_a_malformed_success() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        requested.append(model)
        body = {"unexpected": True} if model == "primary" else completion(model="fallback")
        return httpx.Response(200, json=body)

    caption = asyncio.run(request_caption(handler, attempts=1))

    assert requested == ["primary", "fallback"]
    assert caption.requested_model == "fallback"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"unexpected": True}, "unrecognized error body"),
        (None, "provider returned no error body"),
    ],
)
def test_openrouter_caption_rejects_nonretryable_provider_errors(
    body: dict | None,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json=body) if body is not None else httpx.Response(401, text="")

    with pytest.raises(CaptionError, match=message):
        asyncio.run(request_caption(handler))


def test_openrouter_caption_reports_exhausted_routes_and_validates_policy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    with pytest.raises(CaptionError, match="every image caption route failed"):
        asyncio.run(request_caption(handler, attempts=1))

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(CaptionError, match="HTTP network"):
        asyncio.run(request_caption(offline, attempts=1))

    async def invalid() -> None:
        async with httpx.AsyncClient() as http:
            with pytest.raises(ValueError, match="at least one model"):
                OpenRouterImageCaptioner(http, (), "prompt", 10, 1, 0)
            with pytest.raises(ValueError, match="attempts"):
                OpenRouterImageCaptioner(http, ("model",), "prompt", 10, 0, 0)

    asyncio.run(invalid())


@pytest.mark.parametrize(
    "body",
    [
        ["unexpected"],
        {"error": {"message": 7}},
    ],
)
def test_openrouter_caption_handles_nonstandard_json_errors(body: list[str] | dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json=body)

    with pytest.raises(CaptionError, match="unrecognized error body"):
        asyncio.run(request_caption(handler))


class Captioner:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, str]] = []

    async def caption(self, content: bytes, media_type: str, context: str) -> ImageCaption:
        self.calls.append((content, media_type, context))
        return ImageCaption(
            text="Loss falls as the expert count grows.",
            requested_model="gemma",
            model="gemma",
            provider="CoreWeave",
            elapsed_ms=10,
            usage=CaptionUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            attempts=(
                CaptionAttempt(
                    requested_model="gemma",
                    attempt=1,
                    elapsed_ms=10,
                    status_code=200,
                ),
            ),
        )


def test_description_enricher_replaces_embedded_images_and_deduplicates_bytes() -> None:
    captioner = Captioner()
    image = base64.b64encode(b"same image").decode()
    markdown = (
        f"Before\n\n![loss chart](data:image/jpeg;base64,{image})\n\n"
        f"Again ![](data:image/jpeg;base64,{image}) after."
    )

    described = asyncio.run(
        ImageDescriptionEnricher(captioner, 100).enrich(original(), b"pdf", markdown)
    )

    assert "data:image" not in described.markdown
    assert "Figure description for loss chart" in described.markdown
    assert described.markdown.count("Loss falls as the expert count grows.") == 2
    assert captioner.calls == [(b"same image", "image/jpeg", "loss chart")]
    assert len(described.figures) == 2
    assert described.figures[0].image_sha256 == described.figures[1].image_sha256
    restored = type(described.figures[0]).model_validate(described.metadata()[0])
    assert restored.caption.provider == "CoreWeave"


def test_description_enricher_appends_direct_image_caption_and_leaves_text_alone() -> None:
    captioner = Captioner()
    enricher = ImageDescriptionEnricher(captioner, 100)

    described = asyncio.run(
        enricher.enrich(
            original("image/png").model_copy(update={"companion_text": "architecture"}),
            b"image",
            "OCR text\n",
        )
    )
    untouched = asyncio.run(enricher.enrich(original(), b"pdf", "# Paper\n"))

    assert described.markdown.endswith(
        "## Visual description\n\nLoss falls as the expert count grows.\n"
    )
    assert described.figures[0].alt_text == "architecture"
    assert captioner.calls[0] == (b"image", "image/png", "architecture")
    assert untouched.markdown == "# Paper\n"
    assert untouched.figures == ()


@pytest.mark.parametrize(
    ("content", "limit", "message"),
    [
        (b"", 10, "empty image"),
        (b"too large", 2, "byte limit"),
    ],
)
def test_description_enricher_rejects_unsafe_direct_image_sizes(
    content: bytes,
    limit: int,
    message: str,
) -> None:
    with pytest.raises(CaptionError, match=message):
        asyncio.run(
            ImageDescriptionEnricher(Captioner(), limit).enrich(
                original("image/png"),
                content,
                "",
            )
        )


@pytest.mark.parametrize(
    "markdown",
    [
        "![bad](data:image/png;base64,abc)",
        '<img src="data:image/png;base64,YQ==">',
    ],
)
def test_description_enricher_rejects_invalid_or_unsupported_embedded_data(
    markdown: str,
) -> None:
    with pytest.raises(CaptionError, match="embedded"):
        asyncio.run(
            ImageDescriptionEnricher(Captioner(), 100).enrich(
                original(),
                b"pdf",
                markdown,
            )
        )
