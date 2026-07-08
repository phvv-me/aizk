from dataclasses import dataclass
from enum import StrEnum

from loguru import logger
from openai import APIConnectionError
from patos import FrozenModel
from pydantic import BaseModel, Field, create_model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...exceptions import OntologyNotReadyError
from ...store.models.tables.ontology import EntityKind, RelationKind
from .constants import CONCEPT


@dataclass(frozen=True)
class OntologySnapshot:
    """The live ontology's whole extraction-facing surface, built once from `entity_kind`/
    `relation_kind` and reused until the catalog changes.

    entity_names: every non-structural entity kind, sorted for a byte-stable prompt.
    relation_names: every non-structural relation kind, sorted the same way.
    entity_descriptions: every entity kind's one-line gloss, keyed by name, the auto-create
        cascade's own matching pool.
    entity_description_vectors: `entity_descriptions`' own values, embedded once here rather than
        per suggestion, the auto-create cascade's fixed comparison side.
    llm_entity: one entity's wire shape, `t` constrained to exactly `entity_names`.
    llm_fact: one fact's wire shape, `p` constrained to exactly `relation_names`.
    llm_extraction: the compact wire schema `structured` decodes the combined extraction call
        against, `e`/`f` lists of `llm_entity`/`llm_fact`.
    prompt: the rendered ontology rules fragment, `settings.ontology_prompt_template` filled with
        this snapshot's own type lists.
    """

    entity_names: list[str]
    relation_names: list[str]
    entity_descriptions: dict[str, str]
    entity_description_vectors: dict[str, list[float]]
    llm_entity: type[BaseModel]
    llm_fact: type[BaseModel]
    llm_extraction: type[BaseModel]
    prompt: str


def _wire_schema(
    entity_names: list[str], relation_names: list[str]
) -> tuple[type[BaseModel], type[BaseModel], type[BaseModel]]:
    """Build the combined extraction call's wire schema fresh, its enum fields narrowed to exactly
    the given names, returning `(llm_entity, llm_fact, llm_extraction)`.

    `LLMEntity.t`/`LLMFact.p` are what `structured`'s grammar-constrained decoding actually reads,
    so these, not `ExtractedEntity`/`TimedFact`, are the fields that must track the live catalog.
    Built through `pydantic.create_model` rather than a static class body, since a class body's
    field annotations resolve at import time and this schema's own vocabulary only exists once the
    database has been read, the same reason `EntityGate` waits for its first real construction
    rather than loading its checkpoint at import time.

    entity_names: non-structural entity kind names, the closed `t` vocabulary.
    relation_names: non-structural relation kind names, the closed `p` vocabulary.
    """
    # pyrefly: ignore  # a runtime-built enum from live catalog rows has no literal argument a
    # static functional-enum check could ever verify, the same genuine stub gap this project
    # already carves out for SQLModel's own dynamic column typing.
    entity_type = StrEnum("EntityType", {name: name for name in entity_names})
    # pyrefly: ignore
    relation_type = StrEnum("RelationType", {name: name for name in relation_names})
    llm_entity = create_model(
        "LLMEntity",
        __base__=FrozenModel,
        n=(str, Field(description="plain human-readable noun phrase, never a slug or identifier")),
        t=(entity_type, ...),
        suggested_type=(
            str | None,
            Field(
                default=None,
                description="a more specific type name when t had to fall back to Concept",
            ),
        ),
    )
    llm_fact = create_model(
        "LLMFact",
        __base__=FrozenModel,
        s=(str, ...),
        p=(relation_type, ...),
        o=(str, ""),
        statement=(
            str,
            Field(description="self-contained sentence that stands without the source text"),
        ),
        date=(str | None, None),
    )
    llm_extraction = create_model(
        "LLMExtraction", __base__=FrozenModel, e=(list[llm_entity], ...), f=(list[llm_fact], ...)
    )
    return llm_entity, llm_fact, llm_extraction


async def build_snapshot(session: AsyncSession) -> OntologySnapshot:
    """Read the current catalog and build a fresh `OntologySnapshot` from it.

    Embeds every entity description in one batched call here, once per refresh rather than once
    per auto-create suggestion, since the comparison side of that cascade is fixed between
    refreshes and only the suggestion itself is ever new. An unreachable embed endpoint degrades
    gracefully: the structure still refreshes and only the description vectors are left empty, so a
    transient serving outage cannot block every `ops.setup` the way a hard failure here would.

    session: open session the catalog is read through, `entity_kind`/`relation_kind` carry no
        row level security so any session works.
    """
    from ...serving import Embedder

    entity_names = await EntityKind.extractable_names(session)
    relation_names = await RelationKind.extractable_names(session)
    entity_descriptions = dict(
        (await session.execute(select(EntityKind.name, EntityKind.description))).all()
    )
    try:
        embedded = (
            await Embedder().embed(list(entity_descriptions.values()), mode="document")
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


async def refresh(session: AsyncSession) -> OntologySnapshot:
    """Rebuild and cache the ontology snapshot, the call every ontology write makes afterward.

    session: open session the catalog is read through.
    """
    global _snapshot
    _snapshot = await build_snapshot(session)
    return _snapshot


def current() -> OntologySnapshot:
    """The cached snapshot, raising if `refresh` has never run.

    `ops.setup()` calls `refresh` as its final step, the same bootstrap every server start, worker
    start, and test suite already runs before any real extraction call, so a caller reaching this
    before a single `refresh` genuinely indicates a missed bootstrap rather than a race to paper
    over with a lazy default.
    """
    if _snapshot is None:
        raise OntologyNotReadyError("ontology cache never refreshed, call ops.setup() first")
    return _snapshot


def gate_labels() -> list[str]:
    """Entity kind names the GLiNER2 gate scores a chunk against, `Concept` excluded.

    Concept is the extractor's own catch-all, and calibration against real prose showed it
    matches nearly any noun phrase, which would make the gate pass everything.
    """
    return [name for name in current().entity_names if name != CONCEPT]
