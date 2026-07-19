import os

import dbutil
from id_factory import uuid5, uuid7, uuid8
from sqlalchemy import NullPool, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aizk import ops
from aizk.config import settings
from alembic import command


def migration_url(database: str) -> URL:
    """Point the privileged test connection at one disposable database."""
    return make_url(settings.admin_database_url).set(database=database)


def test_single_init_migration_builds_the_full_schema_and_forces_rls() -> None:
    database = f"aizk_migration_test_{os.getpid()}"
    maintenance_url = migration_url("postgres")
    target_url = migration_url(database)

    async def execute(url: URL, statement: str, params: dict[str, str] | None = None) -> None:
        engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(text(statement), params or {})
        finally:
            await engine.dispose()

    async def body() -> None:
        await execute(
            maintenance_url,
            f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)',
        )
        await execute(maintenance_url, f'CREATE DATABASE "{database}"')
        config = ops.alembic_config()
        config.set_main_option(
            "sqlalchemy.url",
            target_url.render_as_string(hide_password=False).replace("%", "%%"),
        )
        try:
            # The whole schema is one revision, so head installs it in a single upgrade.
            ops.run_alembic(command.upgrade, config, "head")
            document_id = uuid7()
            chunk_id = uuid7()
            owner = uuid5()
            content_hash = uuid8()
            engine = create_async_engine(target_url, poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "INSERT INTO document "
                            "(id, created_by, scopes, title, content_hash) "
                            "VALUES (:id, :owner, ARRAY[:owner]::uuid[], :title, :hash)"
                        ),
                        {
                            "id": str(document_id),
                            "owner": str(owner),
                            "title": "Preserved source",
                            "hash": str(content_hash),
                        },
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO chunk "
                            "(id, document_id, created_by, scopes, ord, text) "
                            "VALUES (:id, :document, :owner, ARRAY[:owner]::uuid[], 0, :text)"
                        ),
                        {
                            "id": str(chunk_id),
                            "document": str(document_id),
                            "owner": str(owner),
                            "text": "Existing production knowledge.",
                        },
                    )
            finally:
                await engine.dispose()

            engine = create_async_engine(target_url, poolclass=NullPool)
            try:
                async with engine.connect() as connection:
                    preserved = (
                        await connection.execute(
                            text(
                                "SELECT title, content_hash, artifact_id, artifact_content_id "
                                "FROM document WHERE id = :id"
                            ),
                            {"id": str(document_id)},
                        )
                    ).one()
                    tables = set(
                        (
                            await connection.execute(
                                text(
                                    "SELECT table_name FROM information_schema.tables "
                                    "WHERE table_schema = 'public' "
                                    "AND table_name = ANY(:names)"
                                ),
                                {
                                    "names": [
                                        "artifact",
                                        "artifact_content",
                                        "blob",
                                        "upload_capability",
                                        "usage_event",
                                    ]
                                },
                            )
                        ).scalars()
                    )
                    forced = dict(
                        (
                            await connection.execute(
                                text(
                                    "SELECT relname, relforcerowsecurity FROM pg_class "
                                    "WHERE relname = ANY(:names)"
                                ),
                                {
                                    "names": [
                                        "artifact",
                                        "artifact_content",
                                        "blob",
                                        "upload_capability",
                                        "usage_event",
                                    ]
                                },
                            )
                        ).all()
                    )
                    chunk_check = (
                        await connection.execute(
                            text(
                                "SELECT pg_get_expr(polwithcheck, polrelid) "
                                "FROM pg_policy "
                                "WHERE polrelid = 'chunk'::regclass "
                                "AND polname = 'scope_insert'"
                            )
                        )
                    ).scalar_one()
                    revision = (
                        await connection.execute(text("SELECT version_num FROM alembic_version"))
                    ).scalar_one()

                assert preserved == (
                    "Preserved source",
                    content_hash,
                    None,
                    None,
                )
                assert tables == {
                    "artifact",
                    "artifact_content",
                    "blob",
                    "upload_capability",
                    "usage_event",
                }
                assert all(forced.values())
                assert "(document_id, scopes) IN" in chunk_check
                assert revision == "0001_init"
            finally:
                await engine.dispose()
        finally:
            await execute(
                maintenance_url,
                f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)',
            )

    dbutil.run(body())
