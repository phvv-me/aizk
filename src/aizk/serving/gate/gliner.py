import asyncio
from collections.abc import Iterable
from functools import cache
from typing import Literal, overload

import httpx
from pydantic import BaseModel

from ...config import settings
from ...extract import ontology
from .contract import (
    ClassifyRequest,
    ClassifyResponse,
    ClassifyTask,
    ExtractRequest,
    ExtractResponse,
)


@cache
def client(variant: str = "") -> httpx.AsyncClient:
    """Reuse one HTTP client per gliner sidecar variant for the process lifetime.

    variant: a named sidecar from `gliner_gate_variants`, the default gliner2
        sidecar at `gliner_gate_url` when empty.
    """
    base_url = settings.gliner_gate_variants[variant] if variant else settings.gliner_gate_url
    return httpx.AsyncClient(base_url=base_url, timeout=settings.gliner_gate_timeout)


@cache
def throttle(variant: str = "") -> asyncio.Semaphore:
    """Reuse one request throttle per sidecar variant for the process lifetime.

    Each sidecar is a single torch process, so a wide fan-out (graph build gates every
    chunk concurrently) must queue client-side instead of piling timeouts onto the model.
    """
    return asyncio.Semaphore(settings.gliner_gate_concurrency)


async def call[R: BaseModel](
    route: str, request: BaseModel, response: type[R], variant: str = ""
) -> R:
    """Post one contract request to a gliner sidecar and validate its reply.

    route: the sidecar's mirrored model route, `/classify` or `/extract`.
    request: the contract request body the route expects.
    response: the contract model the reply validates into.
    variant: which sidecar answers, each container serving exactly one checkpoint.
    """
    async with throttle(variant):
        reply = await client(variant).post(route, json=request.model_dump())
    reply.raise_for_status()
    return response.model_validate(reply.json())


@overload
async def classify[T: str](
    text: str,
    task: str,
    labels: Iterable[T],
    *,
    multi: Literal[False] = False,
    threshold: float | None = None,
) -> T: ...


@overload
async def classify[T: str](
    text: str,
    task: str,
    labels: Iterable[T],
    *,
    multi: Literal[True],
    threshold: float,
) -> set[T]: ...


async def classify[T: str](
    text: str,
    task: str,
    labels: Iterable[T],
    *,
    multi: bool = False,
    threshold: float | None = None,
) -> T | set[T]:
    """Classify text into one or many values from any string label iterable."""
    options = {str(label): label for label in labels}
    tasks: dict[str, list[str] | ClassifyTask] = {
        task: ClassifyTask(labels=list(options), cls_threshold=threshold)
        if multi
        else list(options)
    }
    result = await call("/classify", ClassifyRequest(text=text, tasks=tasks), ClassifyResponse)
    value = result.label(task)
    if multi and isinstance(value, list) and all(item in options for item in value):
        return {options[item] for item in value}
    if not multi and isinstance(value, str) and value in options:
        return options[value]
    raise ValueError(f"GLiNER2 returned invalid labels for {task}")


async def named_entities(text: str) -> list[str]:
    """Extract the entity names a text mentions, lowercased for name matching."""
    extracted = await call(
        "/extract",
        ExtractRequest(
            text=text,
            entity_types=ontology.gate_labels(),
            threshold=settings.gliner_gate_threshold,
        ),
        ExtractResponse,
    )
    spans = (span for group in extracted.entities.values() for span in group)
    return sorted({span.strip().lower() for span in spans if span.strip()})


async def relevant(text: str) -> bool:
    """Return whether text carries an ontology type worth extracting."""
    present = await classify(
        text,
        "present",
        ontology.gate_labels(),
        multi=True,
        threshold=settings.gliner_gate_threshold,
    )
    return bool(present - settings.gliner_gate_floor)
