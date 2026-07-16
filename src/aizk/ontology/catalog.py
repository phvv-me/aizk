from collections.abc import Sequence
from typing import ClassVar

from inflection import underscore
from loguru import logger
from patos import FrozenModel, sql
from sqlalchemy import Integer, Text, column, true
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select

from ..config import settings
from ..exceptions import OntologyNotReadyError
from ..extract.models import ExtractedEntity, TimedFact
from ..serving.embed import embed
from ..store.engine import Session
from ..store.models import Entity, Relation
from .system import System


class Ontology(FrozenModel):
    """Cached extraction vocabulary and database-backed ontology behavior."""

    _cached: ClassVar[Ontology | None] = None

    entity_names: tuple[str, ...]
    relation_names: tuple[str, ...]
    entity_descriptions: dict[str, str]
    relation_descriptions: dict[str, str]
    relation_policies: dict[str, Relation.Policy]
    prompt: str

    def entity_kind(self, name: str) -> str:
        """Return one canonical entity kind by spelling or underscored spelling."""
        key = underscore(name)
        try:
            return next(
                candidate for candidate in self.entity_names if underscore(candidate) == key
            )
        except StopIteration as missing:
            raise ValueError(f"unknown ontology entity type {name!r}") from missing

    def relation_kind(self, name: str) -> str:
        """Return one canonical relation kind by spelling or underscored spelling."""
        key = underscore(name)
        try:
            return next(
                candidate for candidate in self.relation_names if underscore(candidate) == key
            )
        except StopIteration as missing:
            raise ValueError(f"unknown ontology relation {name!r}") from missing

    @property
    def gate_labels(self) -> list[str]:
        """Return extractable entity kinds except the generic concept fallback."""
        return [name for name in self.entity_names if name != System.Entity.CONCEPT]

    @classmethod
    def current(cls) -> Ontology:
        """Return the loaded catalog or fail when process startup skipped it."""
        if cls._cached is None:
            raise OntologyNotReadyError("ontology cache never refreshed, call ops.setup() first")
        return cls._cached

    @classmethod
    async def ensure(cls, session: Session) -> Ontology:
        """Return the loaded catalog, refreshing once for a fresh process."""
        try:
            return cls.current()
        except OntologyNotReadyError:
            return await cls.refresh(session)

    @classmethod
    async def refresh(cls, session: Session) -> Ontology:
        """Refresh missing database embeddings and rebuild the extraction prompt."""
        await cls.embed_missing_kinds(session)
        entity_rows = list(
            await session.exec(
                select(Entity.Kind.name, Entity.Kind.description)
                .where(Entity.Kind.structural.is_(False))
                .order_by(Entity.Kind.name)
            )
        )
        all_relation_rows = list(
            await session.exec(
                select(
                    Relation.Kind.name,
                    Relation.Kind.description,
                    Relation.Kind.policy,
                    Relation.Kind.structural,
                ).order_by(Relation.Kind.name)
            )
        )
        relation_rows = [
            (name, description, policy)
            for name, description, policy, structural in all_relation_rows
            if not structural
        ]
        entity_names = tuple(name for name, _ in entity_rows)
        relation_names = tuple(name for name, _, _ in relation_rows)
        cls._cached = cls(
            entity_names=entity_names,
            relation_names=relation_names,
            entity_descriptions=dict(entity_rows),
            relation_descriptions={name: description for name, description, _ in relation_rows},
            relation_policies={name: policy for name, _, policy, _ in all_relation_rows},
            prompt=settings.ontology_prompt_template.format(
                entity_count=len(entity_names),
                entity_types="\n".join(
                    f"- {name}: {description}" for name, description in entity_rows
                ),
                relation_count=len(relation_names),
                relation_types="\n".join(
                    f"- {name}: {description}" for name, description, _ in relation_rows
                ),
            ),
        )
        return cls._cached

    @classmethod
    def clear(cls) -> None:
        """Clear process state so tests and fresh workers can reload the database catalog."""
        cls._cached = None

    @staticmethod
    async def embed_missing_kinds(session: Session) -> None:
        """Persist embeddings for entity descriptions that do not have one yet."""
        missing = list(
            await session.exec(
                select(Entity.Kind)
                .where(
                    Entity.Kind.structural.is_(False),
                    Entity.Kind.embedding.is_(None),
                )
                .order_by(Entity.Kind.name)
            )
        )
        if not missing:
            return
        embedded = await embed([kind.description for kind in missing], mode="document")
        for kind, vector in zip(missing, embedded, strict=True):
            kind.embedding = vector
        await session.flush()

    @classmethod
    async def define_entity(
        cls,
        session: Session,
        name: str,
        description: str,
        domain: str,
    ) -> Ontology:
        """Create or refine one entity kind and persist its description embedding."""
        [embedding] = await embed([description], mode="document")
        await session.exec(
            insert(Entity.Kind)
            .values(
                name=underscore(name),
                description=description,
                domain=domain,
                embedding=embedding,
            )
            .on_conflict_do_update(
                index_elements=[Entity.Kind.name],
                set_={
                    "description": description,
                    "domain": domain,
                    "embedding": embedding,
                },
            )
        )
        return await cls.refresh(session)

    @classmethod
    async def define_relation(
        cls,
        session: Session,
        name: str,
        description: str,
        domain: str,
        policy: Relation.Policy = Relation.Policy.set,
    ) -> Ontology:
        """Create or refine one relation kind and rebuild the extraction prompt."""
        await session.exec(
            insert(Relation.Kind)
            .values(
                name=underscore(name),
                description=description,
                domain=domain,
                policy=policy,
            )
            .on_conflict_do_update(
                index_elements=[Relation.Kind.name],
                set_={"description": description, "domain": domain, "policy": policy},
            )
        )
        return await cls.refresh(session)

    async def resolve_entity_types(
        self,
        session: Session,
        suggestions: Sequence[tuple[str, list[float]]],
    ) -> dict[str, str]:
        """Resolve every suggested type in one database-ranked cosine query."""
        if not suggestions:
            return {}
        inputs = sql.relation(
            "ontology_suggestions",
            (
                column("ordinal", Integer),
                column("suggestion", Text),
                column("embedding", sql.CosineHalfvec(settings.embed_dim)),
            ),
            [
                (ordinal, suggestion, embedding)
                for ordinal, (suggestion, embedding) in enumerate(suggestions)
            ],
        )
        distance = Entity.Kind.embedding @ inputs.c.embedding
        nearest = (
            select(Entity.Kind.name)
            .where(
                Entity.Kind.structural.is_(False),
                Entity.Kind.embedding.is_not(None),
                distance <= 1.0 - settings.ontology_match_threshold,
            )
            .order_by(distance)
            .limit(1)
            .lateral("nearest_entity_kind")
        )
        rows = await session.exec(
            select(inputs.c.suggestion, nearest.c.name)
            .select_from(inputs.outerjoin(nearest, true()))
            .order_by(inputs.c.ordinal)
        )
        return {
            suggestion: entity_type or System.Entity.CONCEPT for suggestion, entity_type in rows
        }

    @classmethod
    async def normalize(
        cls,
        session: Session,
        entities: Sequence[ExtractedEntity],
        facts: Sequence[TimedFact],
    ) -> tuple[list[ExtractedEntity], list[TimedFact]]:
        """Constrain extracted graph values to the database ontology.

        Unknown entity types become concept suggestions so vector resolution can recover a
        declared type. Facts with unknown predicates are discarded as unsupported claims.
        """
        cls.current()
        entity_types = {underscore(entity.type) for entity in entities}
        predicates = {underscore(fact.predicate) for fact in facts}
        valid_entity_types = set(
            await session.exec(select(Entity.Kind.name).where(Entity.Kind.name.in_(entity_types)))
        )
        valid_predicates = set(
            await session.exec(
                select(Relation.Kind.name).where(Relation.Kind.name.in_(predicates))
            )
        )
        unknown_entity_types = entity_types - valid_entity_types
        unknown_predicates = predicates - valid_predicates
        if unknown_entity_types:
            logger.warning(
                "unknown extracted entity types {}, treating them as concept suggestions",
                sorted(unknown_entity_types),
            )
        if unknown_predicates:
            logger.warning(
                "unknown extracted predicates {}, dropping their facts",
                sorted(unknown_predicates),
            )
        normalized_entities = []
        for entity in entities:
            entity_type = underscore(entity.type)
            normalized_entities.append(
                entity.model_copy(update={"type": entity_type})
                if entity_type in valid_entity_types
                else entity.model_copy(
                    update={
                        "type": System.Entity.CONCEPT,
                        "suggested_type": entity.suggested_type or entity_type,
                    }
                )
            )
        return normalized_entities, [
            fact.model_copy(update={"predicate": predicate})
            for fact in facts
            if (predicate := underscore(fact.predicate)) in valid_predicates
        ]
