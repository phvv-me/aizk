import pytest

from aizk.config import settings
from aizk.store import EntityContent, FactContent, TableBase


def test_validates_rejects_an_off_ontology_predicate() -> None:
    """The ORM boundary refuses a predicate outside the closed relation vocabulary."""
    with pytest.raises(ValueError, match="not in the ontology"):
        FactContent(predicate="resides_in", statement="x")


def test_validates_accepts_an_ontology_predicate() -> None:
    """An in-vocabulary predicate passes the boundary and is stored unchanged."""
    assert FactContent(predicate="related_to", statement="x").predicate == "related_to"


def test_validates_rejects_an_off_ontology_entity_type() -> None:
    """The ORM boundary refuses an entity type outside the closed vocabulary at construction."""
    with pytest.raises(ValueError, match="not in the ontology"):
        EntityContent(name="x", type="Person")


def test_validates_accepts_the_structural_raptor_type() -> None:
    """The structural RAPTOR summary type the tree writes itself passes the boundary."""
    assert EntityContent(name="x", type="RaptorSummary").type == "RaptorSummary"


def test_chunk_tsvector_is_a_persisted_computed_column() -> None:
    """The lexical tsvector is declared once as a stored generated column the schema owns."""
    tsv = TableBase.metadata.tables["chunk"].columns["tsv"]
    assert tsv.computed is not None
    assert tsv.computed.persisted is True


def test_embedding_tables_declare_the_configured_cosine_index() -> None:
    """Each embedded table carries its cosine index under the configured backend, autogenerate's.

    The access method follows `index_backend`, vchordrq by default or hnsw for the portable
    fallback, so the ORM DDL source names the same method the 0001 migration branches to. Entity
    and fact content, the deduplicated tables now carrying the embedding column, replace the old
    combined `entity`/`fact` tables in this roster.
    """
    backend = settings.index_backend
    for table in ("chunk", "entity_content", "fact_content", "community", "profile"):
        indexes = TableBase.metadata.tables[table].indexes
        cosine = [i for i in indexes if i.dialect_options["postgresql"].get("using") == backend]
        assert cosine, f"{table} has no {backend} index in metadata"
        ops = cosine[0].dialect_options["postgresql"]["ops"]
        assert ops == {"embedding": "halfvec_cosine_ops"}
