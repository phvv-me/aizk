import dbutil
import pytest
from id_factory import uuid5, uuid8
from pydantic import ValidationError
from rls import Catalog
from sqlmodel import select

from aizk.config import settings
from aizk.exceptions import NoTenantContext
from aizk.store import Document, Fact, TableBase
from aizk.store.ddl import CreateView, DropView, postgresql_sql
from aizk.store.engine import Database, DatabaseRole, Session
from aizk.store.identity import User
from aizk.store.mixins.scoped import Scoped
from aizk.store.mixins.view import ViewBase


def test_created_at_expression_compiles_to_lower_recorded() -> None:
    compiled = str(select(Fact.Claim.created_at)).lower()
    assert "lower(fact_claim.recorded)" in compiled


def test_view_ddl_round_trips_create_and_drop() -> None:
    view = CreateView(
        Fact.Live.__view_select__(),
        "live_fact",
        postgresql_with={"security_invoker": True},
    )
    ddl = postgresql_sql(view)
    assert ddl.startswith("CREATE VIEW live_fact WITH (security_invoker = true) AS")
    assert postgresql_sql(DropView(view.table, if_exists=True)) == "DROP VIEW IF EXISTS live_fact"


def test_view_ddl_without_options_uses_native_sqlalchemy_rendering() -> None:
    view = CreateView(Fact.Live.__view_select__(), "plain_live_fact")

    assert postgresql_sql(view).startswith("CREATE VIEW plain_live_fact AS")


def test_abstract_store_types_remain_unmapped_and_unregistered() -> None:
    with pytest.raises(NotImplementedError):
        ViewBase.__view_select__()

    class AbstractView(ViewBase):
        pass

    assert getattr(AbstractView, "__mapper__", None) is None
    before = {
        table for table in TableBase.metadata.tables.values() if Catalog.state(table) is not None
    }

    class AbstractScoped(Scoped):
        pass

    after = {
        table for table in TableBase.metadata.tables.values() if Catalog.state(table) is not None
    }
    assert after == before


@pytest.mark.parametrize("role", list(DatabaseRole))
def test_database_uses_a_real_pool_when_not_null_pooled(
    monkeypatch: pytest.MonkeyPatch, role: DatabaseRole
) -> None:
    monkeypatch.setattr(settings, "db_null_pool", False)
    engine = Database(role).engine
    try:
        assert engine.pool.__class__.__name__ != "NullPool"
    finally:
        engine.sync_engine.dispose()


def test_document_record_excludes_derived_columns() -> None:
    doc = Document(content_hash=uuid8(), created_by=uuid5())
    record = doc.record()
    assert doc.id.version == 7
    assert record["table"] == "document"
    assert "embedding" not in record and "tsv" not in record


def test_document_content_identity_requires_uuid8() -> None:
    owner = uuid5()
    digest = uuid8()
    document = Document.model_validate(
        {"content_hash": digest, "created_by": owner, "scopes": [owner]}
    )
    assert document.content_hash == digest

    with pytest.raises(ValidationError, match="UUID version 8 expected"):
        Document.model_validate({"content_hash": uuid5(), "created_by": owner, "scopes": [owner]})


def test_session_requires_a_bound_user() -> None:
    with pytest.raises(NoTenantContext, match="database session has no user"):
        _ = Session().user


def test_scope_boundaries_store_one_nonempty_canonical_key(migrated_db: None) -> None:
    first, second = sorted((uuid5(), uuid5()))
    assert User.authorized(first, read=(second, first, second)).scopes.read == frozenset(
        {first, second}
    )

    async def persist() -> None:
        async with User.system().owner as session:
            document = Document(
                created_by=first,
                scopes=[second, first, second],
                content_hash=uuid8(),
            )
            session.add(document)
            await session.flush()
            stored = await session.scalar(
                select(Document.scopes).where(Document.id == document.id)
            )
            assert document.scopes == stored == [first, second]
            await session.rollback()

        async with User.system().owner as session:
            session.add(Document(created_by=first, scopes=[], content_hash=uuid8()))
            with pytest.raises(ValueError, match="scopes cannot be empty"):
                await session.flush()
            await session.rollback()

    dbutil.run(persist())


def test_direct_source_identity_requires_a_complete_normalized_title(
    migrated_db: None,
) -> None:
    owner = uuid5()

    async def match(query: str, titles: list[str]) -> dict[str, bool]:
        async with dbutil.actor(owner) as session:
            documents = [
                Document(
                    title=title,
                    created_by=owner,
                    scopes=[owner],
                    content_hash=uuid8(),
                )
                for title in titles
            ]
            session.add_all(documents)
            await session.flush()
            rows = await session.exec(
                select(Document.title, Document.named_in_query()).where(
                    Document.id.in_([document.id for document in documents])
                ),
                params={"qtext": query},
            )
            await session.rollback()
        return {title: direct for title, direct in rows}

    assert dbutil.run(
        match(
            "What are the current Research projects in Open SWE Book.md?",
            ["en", "Research", "Open SWE Book.md", "SWE Book"],
        )
    ) == {
        "en": False,
        "Research": True,
        "Open SWE Book.md": True,
        "SWE Book": True,
    }
