from collections import defaultdict
from dataclasses import dataclass
from re import finditer
from typing import Protocol, TypedDict


class Span(TypedDict):
    text: str
    start: int
    end: int
    confidence: float


class Relation(TypedDict):
    head: Span
    tail: Span


class GraphResult(TypedDict):
    entities: dict[str, list[Span]]
    relation_extraction: dict[str, list[Relation]]


class BatchExtractor[SchemaT](Protocol):
    def batch_extract(
        self,
        texts: list[str],
        schemas: SchemaT,
        batch_size: int,
        threshold: float,
        num_workers: int,
        format_results: bool,
        include_confidence: bool,
        include_spans: bool,
    ) -> list[GraphResult]: ...


@dataclass(frozen=True, slots=True)
class TextWindow:
    """One overlapping text window and its character offset in the source."""

    text: str
    offset: int


@dataclass(frozen=True, slots=True)
class LongTextExtractor:
    """Batch overlapping word windows and merge their grounded graph output."""

    window_size: int
    overlap: int
    batch_size: int

    def __post_init__(self) -> None:
        if self.window_size <= self.overlap or self.overlap < 0:
            raise ValueError("window_size must be positive and greater than overlap")

    def extract[SchemaT](
        self,
        model: BatchExtractor[SchemaT],
        text: str,
        schema: SchemaT,
        threshold: float,
    ) -> GraphResult:
        """Extract every window in batches and restore source character spans."""
        windows = self.windows(text)
        return self.merge(
            windows,
            model.batch_extract(
                [window.text for window in windows],
                schema,
                batch_size=self.batch_size,
                threshold=threshold,
                num_workers=0,
                format_results=True,
                include_confidence=True,
                include_spans=True,
            ),
        )

    def windows(self, text: str) -> list[TextWindow]:
        """Split text on word boundaries with enough overlap for boundary relations."""
        words = list(finditer(r"\S+", text))
        if len(words) <= self.window_size:
            return [TextWindow(text, 0)]

        step = self.window_size - self.overlap
        return [
            TextWindow(
                text[
                    words[start].start() : words[
                        min(start + self.window_size, len(words)) - 1
                    ].end()
                ],
                words[start].start(),
            )
            for start in range(0, len(words) - self.overlap, step)
        ]

    @classmethod
    def merge(cls, windows: list[TextWindow], results: list[GraphResult]) -> GraphResult:
        """Remap local spans and keep the strongest copy from overlapping windows."""
        if len(windows) != len(results):
            raise ValueError("each text window must have one extraction result")

        entities: dict[tuple[str, int, int], Span] = {}
        relations: dict[tuple[str, int, int, int, int], Relation] = {}
        for window, result in zip(windows, results, strict=True):
            cls._merge_entities(entities, window, result)
            cls._merge_relations(relations, window, result)
        return GraphResult(
            entities=cls._group_entities(entities),
            relation_extraction=cls._group_relations(relations),
        )

    @classmethod
    def _merge_entities(
        cls,
        entities: dict[tuple[str, int, int], Span],
        window: TextWindow,
        result: GraphResult,
    ) -> None:
        for kind, spans in result.get("entities", {}).items():
            for span in spans:
                shifted = cls._shift(span, window.offset)
                key = (kind, shifted["start"], shifted["end"])
                current = entities.get(key)
                if current is None or shifted["confidence"] > current["confidence"]:
                    entities[key] = shifted

    @classmethod
    def _merge_relations(
        cls,
        relations: dict[tuple[str, int, int, int, int], Relation],
        window: TextWindow,
        result: GraphResult,
    ) -> None:
        for predicate, extracted in result.get("relation_extraction", {}).items():
            for relation in extracted:
                head = cls._shift(relation["head"], window.offset)
                tail = cls._shift(relation["tail"], window.offset)
                key = (predicate, head["start"], head["end"], tail["start"], tail["end"])
                candidate = Relation(head=head, tail=tail)
                current = relations.get(key)
                if current is None or cls._confidence(candidate) > cls._confidence(current):
                    relations[key] = candidate

    @staticmethod
    def _shift(span: Span, offset: int) -> Span:
        return Span(
            text=span["text"],
            start=span["start"] + offset,
            end=span["end"] + offset,
            confidence=span["confidence"],
        )

    @staticmethod
    def _confidence(relation: Relation) -> float:
        return min(relation["head"]["confidence"], relation["tail"]["confidence"])

    @staticmethod
    def _group_entities(entities: dict[tuple[str, int, int], Span]) -> dict[str, list[Span]]:
        grouped_entities: defaultdict[str, list[Span]] = defaultdict(list)
        for (kind, _, _), span in entities.items():
            grouped_entities[kind].append(span)
        for spans in grouped_entities.values():
            spans.sort(key=lambda span: (span["start"], span["end"]))
        return dict(grouped_entities)

    @staticmethod
    def _group_relations(
        relations: dict[tuple[str, int, int, int, int], Relation],
    ) -> dict[str, list[Relation]]:
        grouped_relations: defaultdict[str, list[Relation]] = defaultdict(list)
        for (predicate, *_), relation in relations.items():
            grouped_relations[predicate].append(relation)
        for extracted in grouped_relations.values():
            extracted.sort(
                key=lambda relation: (
                    relation["head"]["start"],
                    relation["tail"]["start"],
                )
            )
        return dict(grouped_relations)
