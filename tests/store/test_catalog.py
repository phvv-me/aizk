import dbutil
import pytest
import rls
from rls import Catalog
from sqlalchemy.ext.asyncio import create_async_engine

from aizk.config import settings
from aizk.store import Document, FactClaim, FactContent, TableBase, verify_rls

pytestmark = pytest.mark.usefixtures("migrated_db")


def test_declarations_live_on_each_protected_table() -> None:
    protected = {
        table.name
        for table in TableBase.metadata.tables.values()
        if Catalog.state(table) is not None
    }
    assert {"document", "fact_claim", "entity_claim", "watermark"} <= protected
    assert {"entity_content", "fact_content"} <= protected
    assert Catalog.state(TableBase.metadata.tables["document"]) is not None


def test_live_schema_forces_rls_with_no_violations() -> None:
    async def body() -> list[str]:
        engine = create_async_engine(settings.admin_database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(verify_rls)
        finally:
            await engine.dispose()

    assert dbutil.run(body()) == []


def test_scoped_models_expose_only_their_declared_commands() -> None:
    policies = Document.__rls__()
    assert {policy.command for policy in policies} == {
        rls.Command.select,
        rls.Command.insert,
        rls.Command.update,
    }
    assert {policy.name for policy in policies} == {
        "scope_read",
        "scope_insert",
        "scope_update",
    }
    assert {policy.name for policy in FactClaim.__rls__()} == {policy.name for policy in policies}


def test_content_visibility_is_readable_and_mintable_only() -> None:
    policies = FactContent.__rls__()
    assert {policy.command for policy in policies} == {rls.Command.select, rls.Command.insert}
    assert policies[0].using is not None
