import uuid

import dbutil
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aizk.config import settings
from aizk.exceptions import OntologyNotReadyError
from aizk.extract import ontology
from aizk.extract.models import ExtractedEntity, TimedFact
from aizk.extract.ontology import cache as ontology_cache
from aizk.graph.ontology_growth import derive_type_name, resolve_suggested_type
from aizk.store import EntityKind, RelationKind, system_session
from aizk.store.models.tables.entity import EntityContent

pytestmark = pytest.mark.usefixtures("migrated_db")


@pytest.fixture
def clean_ontology_growth():
    """Delete every auto-created row this test mints and restore the cache afterward.

    entity_kind/relation_kind carry no row level security and are never among dbutil's per-test
    truncated tables, since they are process-wide catalog data, not per-tenant fixtures, so a test
    that grows the catalog cleans up its own rows rather than leaving them for a later test's
    assertions about the seeded baseline to trip over. An auto-created row is exactly one tagged
    `domain='auto'`, no seeded row ever carries that tag, so it cleanly identifies what to delete.
    """
    yield
    dbutil.run(dbutil.admin_exec("DELETE FROM entity_kind WHERE domain = 'auto'"))
    dbutil.run(dbutil.admin_exec("DELETE FROM relation_kind WHERE domain = 'auto'"))

    async def restore() -> None:
        async with system_session() as session:
            await ontology.refresh(session)

    dbutil.run(restore())


def test_timed_fact_object_alias_round_trips() -> None:
    """`TimedFact` accepts and emits the `object` alias while the attribute stays `object_`."""
    fact = TimedFact.model_validate(
        {"subject": "a", "predicate": "uses", "object": "b", "statement": "a uses b"}
    )
    assert fact.object_ == "b"
    assert fact.model_dump(by_alias=True)["object"] == "b"


def test_timed_fact_defaults_to_open_window() -> None:
    """A `TimedFact` left undated carries a null valid window, the always-holding shape."""
    fact = TimedFact(subject="a", predicate="uses", statement="s")
    assert fact.valid_from is None and fact.valid_to is None
    assert fact.object_ == ""


def test_extracted_entity_suggested_type_defaults_to_none() -> None:
    """An entity the extractor typed confidently carries no suggestion at all."""
    entity = ExtractedEntity(name="Ada", type="Author")
    assert entity.suggested_type is None


def test_extractable_names_exclude_structural_members() -> None:
    """The extraction vocabulary never includes the system-written RAPTOR/observation types."""

    async def body() -> tuple[list[str], list[str]]:
        async with system_session() as session:
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
    """The seed migration lands each kind with its domain tag and structural flag intact."""

    async def body() -> tuple[EntityKind, EntityKind]:
        async with system_session() as session:
            return (
                await session.get_one(EntityKind, ontology.PROJECT),
                await session.get_one(EntityKind, ontology.RAPTOR_SUMMARY),
            )

    project, raptor = dbutil.run(body())
    assert project.domain == "general" and project.structural is True
    assert raptor.structural is True


def test_mint_is_idempotent_on_a_conflicting_name(clean_ontology_growth: None) -> None:
    """Minting an already-present name is a no-op, the first writer's row wins, no unique
    violation, the property that lets two concurrent extractions agree on one row."""

    async def body() -> tuple[int, str]:
        async with system_session() as session:
            await EntityKind.mint(session, name="GrowthProbe", description="first", domain="auto")
            await EntityKind.mint(session, name="GrowthProbe", description="second", domain="auto")
            rows = list(
                await session.scalars(select(EntityKind).where(EntityKind.name == "GrowthProbe"))
            )
        return len(rows), rows[0].description

    count, description = dbutil.run(body())
    assert count == 1
    assert description == "first"  # ON CONFLICT DO NOTHING keeps the original


def test_entity_content_rejects_an_off_vocabulary_type() -> None:
    """Postgres itself refuses a type outside the live catalog, the FK that replaced the old
    hardcoded `CHECK` constraint."""

    async def body() -> None:
        async with system_session() as session:
            session.add(EntityContent(name="x", type="NotARealType"))
            await session.flush()

    with pytest.raises(IntegrityError, match="entity_content_type_fkey"):
        dbutil.run(body())


def test_fact_content_rejects_an_off_vocabulary_predicate() -> None:
    """The same FK wall on the edge side, `fact_content.predicate` against `relation_kind`."""
    from aizk.store import FactContent

    async def body() -> None:
        async with system_session() as session:
            session.add(
                FactContent(subject_id=uuid.uuid4(), predicate="not_a_relation", statement="s")
            )
            await session.flush()

    with pytest.raises(IntegrityError, match="fact_content_predicate_fkey"):
        dbutil.run(body())


def test_current_raises_before_any_refresh_has_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading the cache before `ops.setup()` has ever refreshed it is a loud, named error."""
    monkeypatch.setattr(ontology_cache, "_snapshot", None)
    with pytest.raises(OntologyNotReadyError):
        ontology.current()


def test_gate_labels_exclude_concept_but_include_a_real_member() -> None:
    """The GLiNER2 gate's own label list drops Concept, the catch-all calibration rejected."""
    labels = ontology.gate_labels()
    assert ontology.CONCEPT not in labels
    assert "Decision" in labels


def test_derive_type_name_builds_pascal_case_from_the_first_few_words() -> None:
    """A free-text suggestion folds to a short PascalCase identifier, never a whole sentence."""
    assert derive_type_name("a financial goal for the house") == "AFinancialGoal"
    assert derive_type_name("!!!") == ontology.CONCEPT  # no usable words falls back safely


def test_resolve_suggested_type_folds_into_an_identical_existing_description(
    fake_embedder: object, clean_ontology_growth: None
) -> None:
    """A suggestion textually identical to an existing kind's description folds into it, the
    fake embedder's deterministic vector guaranteeing a perfect cosine match.

    Refreshes the cache first, under the now-installed fake embedder, since
    `entity_description_vectors` was built at suite bootstrap against whatever embedder was
    active then, comparing a fresh fake-embedded suggestion against those would compare two
    unrelated embedding spaces.
    """

    async def body() -> tuple[str, str]:
        async with system_session() as session:
            await ontology.refresh(session)
            # a non-structural kind: structural kinds (RaptorSummary, Observation) are deliberately
            # excluded from the auto-create fold pool, so a suggestion never resolves into one.
            name, description = (
                await session.execute(
                    select(EntityKind.name, EntityKind.description)
                    .where(~EntityKind.structural)
                    .limit(1)
                )
            ).one()
            resolved = await resolve_suggested_type(session, description)
        return resolved, name

    resolved_name, existing_name = dbutil.run(body())
    assert resolved_name == existing_name


def test_resolve_suggested_type_mints_a_new_kind_for_a_novel_suggestion(
    fake_embedder: object, clean_ontology_growth: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suggestion unlike anything known mints a fresh row tagged `domain='auto'`.

    The fake embedder's deterministic vectors are every byte of a SHA256 digest normalized to
    [0, 1], never negative, so any two of its vectors trend toward a real but meaningless
    similarity from that shared non-negative range alone, not from the text's actual content.
    Raising the fold threshold past cosine similarity's own maximum forces the mint branch
    deterministically, regardless of that artifact, so this test asserts the mint plumbing
    itself rather than depending on the fake's incidental geometry.
    """
    monkeypatch.setattr(settings, "ontology_growth_threshold", 1.1)
    suggested = str(uuid.uuid4())

    async def body() -> EntityKind:
        async with system_session() as session:
            await ontology.refresh(session)
            name = await resolve_suggested_type(session, suggested)
            return await session.get_one(EntityKind, name)

    row = dbutil.run(body())
    assert row.domain == "auto"
    assert row.description == suggested
