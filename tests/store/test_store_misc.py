import uuid

import pytest

from aizk.config import settings
from aizk.store import Document, LiveFact
from aizk.store.engine import build_engine
from aizk.store.mixins.view import ViewBase, create_view_ddl, drop_view_ddl
from aizk.store.models.tables.membership import Membership

# entity_content.type/fact_content.predicate no longer validate off-ontology values in memory,
# that wall moved to a real foreign key against the live entity_kind/relation_kind catalog, see
# tests/extract/test_ontology.py's DB-backed rejection tests instead.


def test_created_at_expression_compiles_to_lower_recorded() -> None:
    """The `created_at` hybrid's SQL form is Postgres's `lower(recorded)` on the claim table."""
    from sqlalchemy import select

    from aizk.store import FactClaim

    compiled = str(select(FactClaim.created_at)).lower()
    assert "lower(fact_claim.recorded)" in compiled


def test_view_ddl_round_trips_create_and_drop() -> None:
    """`create_view_ddl` compiles a security_invoker view; `drop_view_ddl` reverses it by name."""
    ddl = create_view_ddl("live_fact", LiveFact.__view_select__())
    assert ddl.startswith("CREATE VIEW live_fact WITH (security_invoker = true) AS")
    assert drop_view_ddl("live_fact") == "DROP VIEW IF EXISTS live_fact"


def test_abstract_view_select_raises() -> None:
    """The `ViewBase` base has no defining select; a subclass must supply its own."""
    with pytest.raises(NotImplementedError):
        ViewBase.__view_select__()


def test_intermediate_view_subclass_without_a_select_is_not_mapped() -> None:
    """A `ViewBase` subclass that declares no `__view_select__` stays an unmapped abstract base."""

    class AbstractView(ViewBase):
        """An intermediate base carrying no defining select, so `register_view` never runs."""

    # register_view (which imperatively maps the class) is skipped, so no mapper is installed
    assert getattr(AbstractView, "__mapper__", None) is None


def test_intermediate_scoped_mixin_registers_no_table() -> None:
    """A `Scoped` mixin subclass with no `__tablename__` registers nothing under the rls set."""
    from aizk.store import TableBase
    from aizk.store.mixins.scoped import Scoped

    before = set(TableBase.metadata.info.get("rls", set()))

    class AbstractScoped(Scoped):
        """An abstract intermediate carrying the scope columns but no concrete table name."""

    assert set(TableBase.metadata.info.get("rls", set())) == before  # nothing registered


def test_writable_group_ids_selects_non_reader_roles() -> None:
    """`writable_group_ids` compiles to a select over the principal's writer/admin memberships."""
    statement = Membership.writable_group_ids(uuid.uuid4())
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "membership" in compiled and "role" in compiled


def test_build_engine_uses_a_real_pool_when_not_null_pooled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `db_null_pool` off, `build_engine` builds the pooled production engine."""
    monkeypatch.setattr(settings, "db_null_pool", False)
    engine = build_engine()
    try:
        assert engine.pool.__class__.__name__ != "NullPool"
    finally:
        # dispose synchronously-safe: the async engine's sync_engine owns the pool
        engine.sync_engine.dispose()


def test_document_record_excludes_derived_columns() -> None:
    """`record` serializes a row to a json-ready dict tagged with its table, dropping vectors."""
    doc = Document(id=uuid.uuid4(), content_hash="h", owner_id=uuid.uuid4(), kind="note")
    record = doc.record()
    assert record["table"] == "document"
    assert "embedding" not in record and "tsv" not in record
