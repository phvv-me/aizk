import uuid

import dbutil
import httpx
import pytest
from openai import APIConnectionError
from sqlalchemy.exc import IntegrityError

from aizk.exceptions import OntologyNotReadyError
from aizk.extract import ontology
from aizk.extract.models import ExtractedEntity, TimedFact
from aizk.extract.ontology import cache as ontology_cache
from aizk.store import EntityKind, FactContent, RelationKind, as_system
from aizk.store.models.tables.entity import EntityContent

pytestmark = pytest.mark.usefixtures("migrated_db")


@pytest.fixture
def clean_defined_kind():
    yield
    dbutil.run(dbutil.admin_exec("DELETE FROM entity_kind WHERE domain = 'test'"))


def test_timed_fact_object_alias_round_trips() -> None:
    fact = TimedFact.model_validate(
        {"subject": "a", "predicate": "uses", "object": "b", "statement": "a uses b"}
    )
    assert fact.object_ == "b"
    assert fact.model_dump(by_alias=True)["object"] == "b"


def test_timed_fact_defaults_to_open_window() -> None:
    fact = TimedFact(subject="a", predicate="uses", statement="s")
    assert fact.valid_from is None and fact.valid_to is None
    assert fact.object_ == ""


def test_extracted_entity_suggested_type_defaults_to_none() -> None:
    entity = ExtractedEntity(name="Ada", type="author")
    assert entity.suggested_type is None


def test_extractable_names_exclude_structural_members() -> None:
    async def body() -> tuple[list[str], list[str]]:
        async with as_system() as session:
            return (
                await EntityKind.extractable_names(session),
                await RelationKind.extractable_names(session),
            )

    entities, relations = dbutil.run(body())
    assert ontology.RAPTOR_SUMMARY not in entities
    assert ontology.OBSERVATION not in entities
    assert ontology.OBSERVES not in relations
    assert ontology.PROJECT not in entities  # structural, declared-only, never extractor-emitted
    assert ontology.CONCEPT in entities  # a real, always-seeded, non-structural member stays


def test_seed_baseline_carries_domain_and_structural() -> None:
    async def body() -> tuple[EntityKind, EntityKind]:
        async with as_system() as session:
            return (
                await session.get_one(EntityKind, ontology.PROJECT),
                await session.get_one(EntityKind, ontology.RAPTOR_SUMMARY),
            )

    project, raptor = dbutil.run(body())
    assert project.domain == "general" and project.structural is True
    assert raptor.structural is True


def test_define_creates_then_refines_a_catalog_row(clean_defined_kind: None) -> None:
    async def body() -> str:
        async with as_system() as session:
            await EntityKind.define(
                session, name="Curated Kind", description="first", domain="test"
            )
            await EntityKind.define(
                session, name="curated_kind", description="second", domain="test"
            )
            row = await session.get_one(EntityKind, "curated_kind")
        return row.description

    assert dbutil.run(body()) == "second"  # ON CONFLICT DO UPDATE refreshes, unlike mint


def test_entity_content_rejects_an_off_vocabulary_type() -> None:
    async def body() -> None:
        async with as_system() as session:
            await EntityContent(name="x", type="NotARealType").mint(session)

    with pytest.raises(IntegrityError, match="entity_content_type_fkey"):
        dbutil.run(body())


def test_fact_content_rejects_an_off_vocabulary_predicate() -> None:
    async def body() -> None:
        async with as_system() as session:
            session.add(
                FactContent(subject_id=uuid.uuid4(), predicate="not_a_relation", statement="s")
            )
            await session.flush()

    with pytest.raises(IntegrityError, match="fact_content_predicate_fkey"):
        dbutil.run(body())


def test_current_raises_before_any_refresh_has_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ontology_cache, "_snapshot", None)
    with pytest.raises(OntologyNotReadyError):
        ontology.current()


def test_ensure_current_loads_a_fresh_process_once(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = ontology.current()
    calls = 0

    async def build(session: object) -> ontology.OntologySnapshot:
        nonlocal calls
        calls += 1
        return existing

    monkeypatch.setattr(ontology_cache, "_snapshot", None)
    monkeypatch.setattr(ontology_cache, "build_snapshot", build)

    async def body() -> tuple[ontology.OntologySnapshot, ontology.OntologySnapshot]:
        async with as_system() as session:
            return (
                await ontology.ensure_current(session),
                await ontology.ensure_current(session),
            )

    first, second = dbutil.run(body())
    assert first is existing and second is existing
    assert calls == 1


def test_snapshot_refresh_keeps_structure_when_description_embedding_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(texts: list[str], mode: str) -> list[list[float]]:
        del texts, mode
        raise APIConnectionError(request=httpx.Request("POST", "http://embed.invalid"))

    monkeypatch.setattr(ontology_cache, "embed", unavailable)

    async def body() -> ontology.OntologySnapshot:
        async with as_system() as session:
            return await ontology_cache.build_snapshot(session)

    snapshot = dbutil.run(body())

    assert snapshot.entity_names
    assert snapshot.entity_description_vectors == {}


def test_gate_labels_exclude_concept_but_include_a_real_member() -> None:
    labels = ontology.gate_labels()
    assert ontology.CONCEPT not in labels
    assert "decision" in labels
