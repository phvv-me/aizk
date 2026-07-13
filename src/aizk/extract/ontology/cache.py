from collections.abc import Callable
from enum import StrEnum
from typing import cast

from loguru import logger
from openai import APIConnectionError
from patos import FrozenModel
from pydantic import Field
from pydantic.main import create_model
from sqlmodel import select

from ...config import settings
from ...exceptions import OntologyNotReadyError
from ...provenance import EpistemicKind
from ...serving import embed
from ...store.engine import Session
from ...store.models.tables.ontology import EntityKind, RelationKind
from .constants import CONCEPT

_str_enum = cast(Callable[[str, dict[str, str]], type[StrEnum]], StrEnum)


class WireEntity(FrozenModel):
    """Entity fields shared by every live ontology extraction schema."""

    n: str = Field(description="plain human-readable noun phrase, never a slug or identifier")
    t: str
    suggested_type: str | None = Field(
        default=None,
        description="a more specific type name when t had to fall back to Concept",
    )


class WireFact(FrozenModel):
    """Fact fields shared by every live ontology extraction schema."""

    s: str
    p: str
    o: str = ""
    statement: str = Field(description="self-contained sentence that stands without source text")
    quote: str | None = Field(
        default=None,
        description="shortest verbatim excerpt copied exactly from the text supporting this fact",
    )
    date: str | None = None
    k: EpistemicKind = EpistemicKind.world


class WireExtraction(FrozenModel):
    """Combined entity and fact response shared by every live ontology schema."""

    e: list[WireEntity]
    f: list[WireFact]


class OntologySnapshot(FrozenModel):
    """The live ontology's whole extraction-facing surface, built once from `entity_kind`/
    `relation_kind` and reused until the catalog changes."""

    entity_names: list[str]
    relation_names: list[str]
    entity_descriptions: dict[str, str]
    entity_description_vectors: dict[str, list[float]]
    llm_entity: type[WireEntity]
    llm_fact: type[WireFact]
    llm_extraction: type[WireExtraction]
    prompt: str


def _wire_schema(
    entity_names: list[str], relation_names: list[str]
) -> tuple[type[WireEntity], type[WireFact], type[WireExtraction]]:
    """Build the combined extraction call's wire schema fresh, its enum fields narrowed to
    exactly the given names, returning `(llm_entity, llm_fact, llm_extraction)`."""
    entity_type = _str_enum("EntityType", {name: name for name in entity_names})
    relation_type = _str_enum("RelationType", {name: name for name in relation_names})
    llm_entity = create_model(
        "LLMEntity",
        __base__=WireEntity,
        t=(entity_type, ...),
    )
    llm_fact = create_model(
        "LLMFact",
        __base__=WireFact,
        p=(relation_type, ...),
    )
    llm_extraction = create_model(
        "LLMExtraction",
        __base__=WireExtraction,
        e=(list[llm_entity], ...),
        f=(list[llm_fact], ...),
    )
    return llm_entity, llm_fact, llm_extraction


async def build_snapshot(session: Session) -> OntologySnapshot:
    """Read the current catalog and build a fresh `OntologySnapshot` from it."""
    entity_names = await EntityKind.extractable_names(session)
    relation_names = await RelationKind.extractable_names(session)
    entity_descriptions = dict(
        (
            await session.exec(
                select(EntityKind.name, EntityKind.description)
                .where(EntityKind.structural.is_(False))
                .order_by(EntityKind.name)
            )
        ).all()
    )
    try:
        embedded = (
            await embed(list(entity_descriptions.values()), mode="document")
            if entity_descriptions
            else []
        )
        description_vectors = dict(zip(entity_descriptions, embedded, strict=True))
    except APIConnectionError:
        logger.warning(
            "ontology embed endpoint unreachable, refreshing structure without description vectors"
        )
        description_vectors = {}
    prompt = settings.ontology_prompt_template.format(
        entity_count=len(entity_names),
        entity_types=", ".join(entity_names),
        relation_count=len(relation_names),
        relation_types=", ".join(relation_names),
    )
    llm_entity, llm_fact, llm_extraction = _wire_schema(entity_names, relation_names)
    return OntologySnapshot(
        entity_names=entity_names,
        relation_names=relation_names,
        entity_descriptions=entity_descriptions,
        entity_description_vectors=description_vectors,
        llm_entity=llm_entity,
        llm_fact=llm_fact,
        llm_extraction=llm_extraction,
        prompt=prompt,
    )


_snapshot: OntologySnapshot | None = None


async def refresh(session: Session) -> OntologySnapshot:
    """Rebuild and cache the ontology snapshot, the call every ontology write makes
    afterward."""
    global _snapshot
    _snapshot = await build_snapshot(session)
    return _snapshot


def current() -> OntologySnapshot:
    """The cached snapshot, raising if `refresh` has never run."""
    if _snapshot is None:
        raise OntologyNotReadyError("ontology cache never refreshed, call ops.setup() first")
    return _snapshot


async def ensure_current(session: Session) -> OntologySnapshot:
    """Return the current snapshot, loading it once for a fresh process."""
    try:
        return current()
    except OntologyNotReadyError:
        return await refresh(session)


def gate_labels() -> list[str]:
    """Entity kind names the GLiNER2 gate scores a chunk against, `Concept` excluded."""
    return [name for name in current().entity_names if name != CONCEPT]
