import dbutil
import httpx
import pytest
from doubles import RecordingEmbedder
from id_factory import uuid5
from openai import APIConnectionError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from aizk.exceptions import OntologyNotReadyError
from aizk.extract.models import ExtractedEntity, TimedFact
from aizk.ontology import Ontology, System
from aizk.ontology import catalog as ontology_catalog
from aizk.store import Entity, Fact, Relation
from aizk.store.engine import Session
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")


@pytest.fixture
def clean_defined_kind():
    yield
    dbutil.run(dbutil.admin_exec("DELETE FROM entity_kind WHERE domain = 'test'"))


def test_extraction_models_preserve_aliases_and_open_defaults() -> None:
    fact = TimedFact.model_validate(
        {"subject": "a", "predicate": "uses", "object": "b", "statement": "a uses b"}
    )
    assert fact.object_ == "b"
    assert fact.model_dump(by_alias=True)["object"] == "b"
    open_fact = TimedFact(subject="a", predicate="uses", statement="s")
    assert open_fact.valid_from is None and open_fact.valid_to is None
    assert open_fact.object_ == ""
    entity = ExtractedEntity(name="Ada", type="author")
    assert entity.suggested_type is None


def test_extractable_names_exclude_structural_members() -> None:
    ontology = Ontology.current()
    entities = ontology.entity_names
    relations = ontology.relation_names
    assert System.Entity.RAPTOR_SUMMARY not in entities
    assert System.Entity.OBSERVATION not in entities
    assert System.Relation.OBSERVES not in relations
    assert "project" in entities
    assert System.Entity.CONCEPT in entities  # a real, always-seeded, non-structural member stays
    assert tuple(ontology.entity_descriptions) == entities
    assert tuple(ontology.relation_descriptions) == relations
    assert all(ontology.entity_descriptions.values())
    assert all(ontology.relation_descriptions.values())
    assert "- decision: A choice made and the reasoning behind it." in ontology.prompt
    assert "- uses: A method or experiment employs a tool, dataset, or model." in ontology.prompt
    assert System.Relation.RELATED_TO in ontology.relation_names
    assert ontology.relation_policies["uses"] == Relation.Policy.set
    assert ontology.relation_policies["part_of"] == Relation.Policy.set
    assert ontology.relation_policies["has_status"] == Relation.Policy.state
    assert System.Entity.CONCEPT not in ontology.gate_labels
    assert "decision" in ontology.gate_labels


def test_catalog_rejects_unknown_entity_and_relation_names() -> None:
    ontology = Ontology.current()

    with pytest.raises(ValueError, match="unknown ontology entity type"):
        ontology.entity_kind("imaginary kind")
    with pytest.raises(ValueError, match="unknown ontology relation"):
        ontology.relation_kind("imaginary relation")


def test_normalize_uses_database_vocabulary_for_graph_values() -> None:
    async def body() -> tuple[list[ExtractedEntity], list[TimedFact]]:
        async with User.system() as session:
            return await Ontology.normalize(
                session,
                [
                    ExtractedEntity(name="Ada", type="Author"),
                    ExtractedEntity(name="Source", type="File"),
                    ExtractedEntity(name="Project", type="Project"),
                ],
                [
                    TimedFact(subject="Ada", predicate="RelatedTo", statement="Ada writes"),
                    TimedFact(subject="Source", predicate="References", statement="Source cites"),
                    TimedFact(subject="Project", predicate="Observes", statement="Work happened"),
                ],
            )

    entities, facts = dbutil.run(body())
    assert [(entity.type, entity.suggested_type) for entity in entities] == [
        ("author", None),
        (System.Entity.CONCEPT, "file"),
        ("project", None),
    ]
    assert [fact.predicate for fact in facts] == [
        System.Relation.RELATED_TO,
        System.Relation.OBSERVES,
    ]


def test_seed_baseline_carries_domain_and_structural() -> None:
    async def body() -> tuple[Entity.Kind, Entity.Kind]:
        async with User.system() as session:
            return (
                await session.get_one(Entity.Kind, "project"),
                await session.get_one(Entity.Kind, System.Entity.RAPTOR_SUMMARY),
            )

    project, raptor = dbutil.run(body())
    assert project.domain == "general" and project.structural is False
    assert raptor.structural is True


def test_define_creates_then_refines_a_catalog_row(
    clean_defined_kind: None, fake_embedder: RecordingEmbedder
) -> None:
    del fake_embedder

    async def body() -> str:
        async with User.system() as session:
            await Ontology.define_entity(
                session, name="Declared Kind", description="first", domain="test"
            )
            await Ontology.define_entity(
                session, name="declared_kind", description="second", domain="test"
            )
            row = await session.get_one(Entity.Kind, "declared_kind")
        return row.description

    assert dbutil.run(body()) == "second"  # ON CONFLICT DO UPDATE refreshes, unlike mint


def test_entity_content_rejects_an_off_vocabulary_type() -> None:
    async def body() -> None:
        async with User.system() as session:
            await Entity.Content(id=uuid5(), name="x", type="NotARealType").mint(session)

    with pytest.raises(IntegrityError, match="entity_content_type_fkey"):
        dbutil.run(body())


def test_fact_content_rejects_an_off_vocabulary_predicate() -> None:
    async def body() -> None:
        async with User.system() as session:
            session.add(
                Fact.Content(
                    id=uuid5(),
                    subject_id=uuid5(),
                    predicate="not_a_relation",
                    statement="s",
                )
            )
            await session.flush()

    with pytest.raises(IntegrityError, match="fact_content_predicate_fkey"):
        dbutil.run(body())


def test_cache_requires_refresh_then_loads_a_fresh_process_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = Ontology.current()
    monkeypatch.setattr(Ontology, "_cached", existing)
    Ontology.clear()
    with pytest.raises(OntologyNotReadyError):
        Ontology.current()

    calls = 0

    async def refresh(cls: type[Ontology], session: Session) -> Ontology:
        del session
        nonlocal calls
        calls += 1
        cls._cached = existing
        return existing

    monkeypatch.setattr(Ontology, "_cached", None)
    monkeypatch.setattr(Ontology, "refresh", classmethod(refresh))

    async def body() -> tuple[Ontology, Ontology]:
        async with User.system() as session:
            return (
                await Ontology.ensure(session),
                await Ontology.ensure(session),
            )

    first, second = dbutil.run(body())
    assert first is existing and second is existing
    assert calls == 1


def test_refresh_propagates_description_embedding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(texts: list[str], mode: str) -> list[list[float]]:
        del texts, mode
        raise APIConnectionError(request=httpx.Request("POST", "http://embed.invalid"))

    monkeypatch.setattr(ontology_catalog, "embed", unavailable)

    async def body() -> None:
        async with User.system() as session:
            await session.exec(
                update(Entity.Kind)
                .where(Entity.Kind.name == "decision")
                .values(embedding=None)
                .execution_options(synchronize_session=False)
            )
            await Ontology.refresh(session)

    with pytest.raises(APIConnectionError):
        dbutil.run(body())


def test_refresh_persists_missing_description_embeddings(
    fake_embedder: RecordingEmbedder,
) -> None:
    async def body() -> list[float] | None:
        async with User.system() as session:
            original = (
                await session.exec(
                    select(Entity.Kind.embedding).where(Entity.Kind.name == "decision")
                )
            ).one()
            await session.exec(
                update(Entity.Kind)
                .where(Entity.Kind.name == "decision")
                .values(embedding=None)
                .execution_options(synchronize_session=False)
            )
            await Ontology.refresh(session)
            refreshed = (
                await session.exec(
                    select(Entity.Kind.embedding).where(Entity.Kind.name == "decision")
                )
            ).one()
            await session.exec(
                update(Entity.Kind)
                .where(Entity.Kind.name == "decision")
                .values(embedding=original)
                .execution_options(synchronize_session=False)
            )
            return refreshed

    assert dbutil.run(body()) is not None


def test_suggested_types_are_resolved_by_database_vector_distance() -> None:
    async def body() -> tuple[dict[str, str], dict[str, str]]:
        async with User.system() as session:
            vector = (
                await session.exec(
                    select(Entity.Kind.embedding).where(Entity.Kind.name == "decision")
                )
            ).one()
            assert vector is not None
            return (
                await Ontology.current().resolve_entity_types(
                    session,
                    [
                        ("specific decision", list(vector)),
                        ("unmatched kind", [-value for value in vector]),
                    ],
                ),
                await Ontology.current().resolve_entity_types(session, []),
            )

    resolved, empty = dbutil.run(body())
    assert resolved == {
        "specific decision": "decision",
        "unmatched kind": System.Entity.CONCEPT,
    }
    assert empty == {}
