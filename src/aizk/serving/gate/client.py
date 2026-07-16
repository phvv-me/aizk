from collections.abc import Iterable
from typing import Literal, overload

from ...config import settings
from ...ontology import Ontology
from ..base import HttpService, http_client, request_throttle
from .models import (
    ClassifyRequest,
    ClassifyResponse,
    ClassifyTask,
    ExtractRequest,
    ExtractResponse,
)


class GateClient(HttpService):
    """Classification and mention extraction through one GLiNER sidecar."""

    @classmethod
    def configured(cls, variant: str = "") -> GateClient:
        """Build the requested sidecar client from live settings."""
        url = settings.gliner_variants[variant] if variant else settings.gliner_url
        return cls(
            http_client(url, "", settings.gliner_timeout),
            request_throttle(url, settings.gliner_concurrency),
        )

    @staticmethod
    def _single[LabelT: str](
        task: str,
        value: str | list[str] | None,
        options: dict[str, LabelT],
    ) -> LabelT:
        """Validate and restore one typed label returned by GLiNER."""
        if not isinstance(value, str) or value not in options:
            raise ValueError(f"GLiNER returned invalid labels for {task}")
        return options[value]

    @staticmethod
    def _multiple[LabelT: str](
        task: str,
        value: str | list[str] | None,
        options: dict[str, LabelT],
    ) -> set[LabelT]:
        """Validate and restore a typed label set returned by GLiNER."""
        if not isinstance(value, list) or not all(item in options for item in value):
            raise ValueError(f"GLiNER returned invalid labels for {task}")
        return {options[item] for item in value}

    @overload
    async def classify[LabelT: str](
        self,
        text: str,
        task: str,
        labels: Iterable[LabelT],
        *,
        multi: Literal[False] = False,
        threshold: float | None = None,
    ) -> LabelT: ...

    @overload
    async def classify[LabelT: str](
        self,
        text: str,
        task: str,
        labels: Iterable[LabelT],
        *,
        multi: Literal[True],
        threshold: float,
    ) -> set[LabelT]: ...

    async def classify[LabelT: str](
        self,
        text: str,
        task: str,
        labels: Iterable[LabelT],
        *,
        multi: bool = False,
        threshold: float | None = None,
    ) -> LabelT | set[LabelT]:
        """Classify text into one or many values from a typed label set."""
        options = {str(label): label for label in labels}
        tasks: dict[str, list[str] | ClassifyTask] = {
            task: ClassifyTask(labels=list(options), cls_threshold=threshold)
            if multi
            else list(options)
        }
        result = await self.post(
            "/classify",
            ClassifyRequest(text=text, tasks=tasks),
            ClassifyResponse,
        )
        value = result.label(task)
        return (
            self._multiple(task, value, options) if multi else self._single(task, value, options)
        )

    async def named_entities(self, text: str) -> list[str]:
        """Return normalized unique names mentioned in text."""
        extracted = await self.post(
            "/extract",
            ExtractRequest(
                text=text,
                entity_types=Ontology.current().gate_labels,
                threshold=settings.gliner_gate_threshold,
            ),
            ExtractResponse,
        )
        spans = (span for group in extracted.entities.values() for span in group)
        return sorted({span.strip().lower() for span in spans if span.strip()})

    async def relevant(self, text: str) -> bool:
        """Return whether text carries an extractable ontology type."""
        present = await self.classify(
            text,
            "present",
            Ontology.current().gate_labels,
            multi=True,
            threshold=settings.gliner_gate_threshold,
        )
        return bool(present - settings.gliner_gate_floor)


@overload
async def classify[LabelT: str](
    text: str,
    task: str,
    labels: Iterable[LabelT],
    *,
    multi: Literal[False] = False,
    threshold: float | None = None,
) -> LabelT: ...


@overload
async def classify[LabelT: str](
    text: str,
    task: str,
    labels: Iterable[LabelT],
    *,
    multi: Literal[True],
    threshold: float,
) -> set[LabelT]: ...


async def classify[LabelT: str](
    text: str,
    task: str,
    labels: Iterable[LabelT],
    *,
    multi: bool = False,
    threshold: float | None = None,
) -> LabelT | set[LabelT]:
    """Classify text through the configured sidecar."""
    client = GateClient.configured()
    if multi:
        if threshold is None:
            raise ValueError("multi-label classification needs a threshold")
        return await client.classify(
            text,
            task,
            labels,
            multi=True,
            threshold=threshold,
        )
    return await client.classify(
        text,
        task,
        labels,
        multi=False,
        threshold=threshold,
    )


async def named_entities(text: str) -> list[str]:
    """Return normalized unique names mentioned in text."""
    return await GateClient.configured().named_entities(text)


async def relevant(text: str) -> bool:
    """Return whether text carries an extractable ontology type."""
    return await GateClient.configured().relevant(text)
