import abc

from loguru import logger
from patos import FrozenFlexModel
from pydantic_ai.exceptions import ModelHTTPError

from ..config import Settings, settings
from ..ontology import Ontology, System, WireExtraction
from ..serving.chunk import chunk_text
from ..serving.extract import LLM, GraphBackend, GraphResponse, Relation, Span
from .dates import resolve_valid_from
from .models import ExtractedEntity, Extraction, TimedFact


class Extractor(FrozenFlexModel, abc.ABC):
    """Ontology-constrained graph extractor selected by configuration."""

    @property
    def requires_gate(self) -> bool:
        """Whether graph building should run the cheap relevance gate first."""
        return False

    @abc.abstractmethod
    async def extract(self, text: str) -> Extraction:
        """Extract entities and dated facts from one source span."""

    @classmethod
    def configured(cls, config: Settings, llm: LLM, gliner: GraphBackend) -> Extractor:
        """Build the backend selected in settings over the runtime's shared clients."""
        if config.extract_backend == "gliner":
            return GLiNERExtractor(gliner=gliner)
        return LLMExtractor(llm=llm)


class LLMExtractor(Extractor):
    """Generate one rich graph slice through a schema-constrained LLM."""

    llm: LLM

    @property
    def requires_gate(self) -> bool:
        return True

    @staticmethod
    def system_prompt() -> str:
        """Build the extraction prompt from the live ontology and fixed graph rules."""
        return f"{Ontology.current().prompt}\n{settings.extract_system_prompt}".strip()

    async def extract(self, text: str) -> Extraction:
        extracted = [
            wire
            for window in chunk_text(text, settings.extract_window_size)
            for wire in await self._extract_bounded(window)
        ]
        extraction = Extraction(
            entities=[
                ExtractedEntity(
                    name=entity.n,
                    type=entity.t,
                    suggested_type=entity.suggested_type,
                )
                for wire in extracted
                for entity in wire.e
            ],
            facts=[
                TimedFact(
                    subject=fact.s,
                    predicate=fact.p,
                    object=fact.o,
                    statement=fact.statement,
                    quote=fact.quote,
                    valid_from=resolve_valid_from(fact.date, fact.statement),
                    kind=fact.k,
                )
                for wire in extracted
                for fact in wire.f
            ],
        )
        self.log(extraction, text)
        return extraction

    async def _extract_window(self, text: str) -> WireExtraction:
        """Extract one source window within the model's bounded context."""
        return await self.llm.generate(
            self.system_prompt(),
            f"<document>\n{text}\n</document>",
            WireExtraction,
            max_tokens=settings.llm_extract_max_tokens,
        )

    async def _extract_bounded(self, text: str) -> list[WireExtraction]:
        """Retry only a context-overflowing window as smaller source spans."""
        try:
            return [await self._extract_window(text)]
        except ModelHTTPError as error:
            if not self._context_overflow(error) or len(text) < 2:
                raise
            spans = chunk_text(text, max(1, len(text) // 2))
            if len(spans) < 2:
                raise
            logger.warning(
                "extractor context overflow for {} chars, retrying as {} bounded spans",
                len(text),
                len(spans),
            )
            return [wire for span in spans for wire in await self._extract_bounded(span)]

    @staticmethod
    def _context_overflow(error: ModelHTTPError) -> bool:
        """Recognize the vLLM context-limit response without masking other HTTP failures."""
        if error.status_code != 400:
            return False
        if isinstance(error.body, str):
            message = error.body
        elif isinstance(error.body, dict):
            value = error.body.get("message")
            message = value if isinstance(value, str) else ""
        else:
            message = ""
        return "maximum context length" in message.casefold()

    @staticmethod
    def log(extraction: Extraction, text: str) -> None:
        """Record one extractor result without coupling callers to its implementation."""
        logger.info(
            "extracted {} entities and {} facts from {} chars",
            len(extraction.entities),
            len(extraction.facts),
            len(text),
        )


class GLiNERExtractor(Extractor):
    """Build a grounded graph slice from GLiNER entities and relations."""

    gliner: GraphBackend

    @staticmethod
    def _excerpt(text: str, head: Span, tail: Span) -> str:
        """Return the smallest sentence-like source span covering both relation ends."""
        start = max(0, min(head.start, tail.start))
        end = min(len(text), max(head.end, tail.end))
        left = max(text.rfind(boundary, 0, start) for boundary in "\n.!?") + 1
        endings = [
            position + 1 for boundary in "\n.!?" if (position := text.find(boundary, end)) >= 0
        ]
        right = min(endings, default=len(text))
        return text[left:right].strip()

    @staticmethod
    def _entity(span: Span, entity_type: str) -> ExtractedEntity:
        """Convert one grounded GLiNER span to an extracted entity."""
        return ExtractedEntity(
            name=span.text.strip(),
            type=entity_type,
            attributes={"confidence": span.confidence},
        )

    def convert(self, text: str, result: GraphResponse) -> Extraction:
        """Convert GLiNER's grounded graph wire shape into Aizk's graph slice."""
        ranked_entities = self._ranked_entities(result)
        ranked_relations = self._ranked_relations(result)
        detected_types = {
            span.text.strip().casefold(): entity_type
            for span, entity_type in reversed(ranked_entities)
        }
        extraction = Extraction(
            entities=self._entities(ranked_entities, ranked_relations, detected_types),
            facts=self._facts(text, ranked_relations),
        )
        LLMExtractor.log(extraction, text)
        return extraction

    @staticmethod
    def _ranked_entities(result: GraphResponse) -> list[tuple[Span, str]]:
        return sorted(
            (
                (span, entity_type)
                for entity_type, spans in result.entities.items()
                for span in spans
                if span.text.strip()
            ),
            key=lambda item: item[0].confidence,
            reverse=True,
        )

    @staticmethod
    def _ranked_relations(result: GraphResponse) -> list[tuple[str, Relation]]:
        return sorted(
            (
                (predicate, relation)
                for predicate, relations in result.relation_extraction.items()
                for relation in relations
                if relation.head.text.strip()
                and relation.tail.text.strip()
                and relation.head.text.strip().casefold() != relation.tail.text.strip().casefold()
            ),
            key=lambda item: min(item[1].head.confidence, item[1].tail.confidence),
            reverse=True,
        )[:8]

    def _entities(
        self,
        ranked_entities: list[tuple[Span, str]],
        ranked_relations: list[tuple[str, Relation]],
        detected_types: dict[str, str],
    ) -> list[ExtractedEntity]:
        entities: dict[str, ExtractedEntity] = {}
        for _, relation in ranked_relations:
            for span in (relation.head, relation.tail):
                name = span.text.strip()
                key = name.casefold()
                entities.setdefault(
                    key,
                    self._entity(span, detected_types.get(key, System.Entity.CONCEPT)),
                )
        for span, entity_type in ranked_entities:
            if len(entities) == 16:
                break
            entity = self._entity(span, entity_type)
            entities.setdefault(entity.name.casefold(), entity)
        return list(entities.values())

    def _facts(self, text: str, ranked_relations: list[tuple[str, Relation]]) -> list[TimedFact]:
        facts: dict[tuple[str, str, str, str], TimedFact] = {}
        for predicate, relation in ranked_relations:
            subject = relation.head.text.strip()
            object_name = relation.tail.text.strip()
            excerpt = self._excerpt(text, relation.head, relation.tail)
            statement = excerpt or f"{subject} {predicate.replace('_', ' ')} {object_name}."
            fact = TimedFact(
                subject=subject,
                predicate=predicate,
                object=object_name,
                statement=statement,
                quote=excerpt or None,
                valid_from=resolve_valid_from(None, statement),
            )
            facts.setdefault(
                (subject.casefold(), predicate, object_name.casefold(), statement),
                fact,
            )
        return list(facts.values())

    async def extract(self, text: str) -> Extraction:
        return self.convert(text, await self.gliner.extract(text))
