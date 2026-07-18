from collections.abc import Awaitable, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import cast

import dbutil
import mcp.types as mt
import mcp_probe
import pytest
from factories import artifact_blob, artifact_content
from fastmcp.exceptions import ResourceError
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.server.context import Context
from fastmcp.server.middleware import MiddlewareContext
from id_factory import uuid5, uuid7
from mcp_probe import context_for
from obstore.exceptions import BaseError as ObjectStoreError
from obstore.store import MemoryStore
from patos import sql
from pydantic import UUID5, UUID7
from sqlalchemy import text

from aizk.storage import ByteStore
from aizk.store import Artifact, Blob
from aizk.store.identity import User

type ResourceReader = Callable[[UUID7, UUID7, Context], Awaitable[ResourceResult]]


async def read_through_transport(
    read_artifact: ResourceReader,
    artifact_id: UUID7,
    content_id: UUID7,
    user: User,
) -> ResourceResult:
    """Read one artifact revision through the identity middleware inside a server span."""
    request = cast(
        "MiddlewareContext[mt.ReadResourceRequestParams]",
        SimpleNamespace(
            fastmcp_context=context_for(),
            method="resources/read",
            message=mt.ReadResourceRequestParams(
                uri=f"aizk://artifacts/{artifact_id}/contents/{content_id}"
            ),
        ),
    )

    async def call_next(
        context: MiddlewareContext[mt.ReadResourceRequestParams],
    ) -> ResourceResult:
        assert context.fastmcp_context is not None
        return await read_artifact(artifact_id, content_id, context.fastmcp_context)

    middleware = mcp_probe.transport_middleware(user)
    return await mcp_probe.through_transport(
        lambda: middleware.on_read_resource(request, call_next)
    )


async def usage_events() -> list[tuple[str, int, list[UUID5], list[UUID5], UUID5]]:
    """Read every recorded usage event in creation order straight from PostgreSQL."""
    async with dbutil.admin_engine().connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT operation, response_bytes, targets, scopes, created_by "
                "FROM usage_event ORDER BY id"
            )
        )
        return [tuple(row) for row in rows.all()]


pytestmark = pytest.mark.usefixtures("migrated_db")


def test_artifact_resource_template_is_the_only_binary_read_surface() -> None:
    templates = dbutil.run(mcp_probe.server.list_resource_templates())

    [template] = templates
    assert [(template.name, template.uri_template) for template in templates] == [
        (
            "artifact",
            "aizk://artifacts/{artifact_id}/contents/{artifact_content_id}",
        )
    ]
    assert template.parameters["properties"] == {
        "artifact_id": {"format": "uuid7", "type": "string"},
        "artifact_content_id": {"format": "uuid7", "type": "string"},
    }
    assert set(template.parameters["required"]) == {
        "artifact_id",
        "artifact_content_id",
    }


def test_artifact_resource_reads_exact_visible_original_and_verifies_integrity() -> None:
    owner, outsider = uuid5(), uuid5()
    reader, stranger = User.private(owner), User.private(outsider)
    store = ByteStore(
        backend=MemoryStore(),
        upload_byte_limit=1024,
        internal_download_lifetime=timedelta(minutes=5),
    )
    read_artifact = mcp_probe.build_server(store=store).artifact_resource()

    async def body() -> None:
        await dbutil.reset_db()
        mcp_probe.captured.clear()
        old = await store.put(b"old bytes")
        latest = await store.put(b"latest bytes")

        artifact = Artifact(name="paper.pdf", created_by=owner, scopes=[owner])
        old_blob = artifact_blob(stored=old, media_type="application/pdf")
        latest_blob = artifact_blob(stored=latest, media_type="application/pdf")
        async with User.private(owner) as session:
            session.add_all((artifact, old_blob, latest_blob))
            await session.flush()
            ready = Artifact.Content.State.ready
            old_content = artifact_content(
                artifact.id, old_blob.id, owner, [owner], revision=1, state=ready
            )
            latest_content = artifact_content(
                artifact.id, latest_blob.id, owner, [owner], revision=2, state=ready
            )
            session.add_all(
                (
                    old_content,
                    latest_content,
                )
            )

        old_result = await read_through_transport(
            read_artifact,
            artifact.id,
            old_content.id,
            reader,
        )
        latest_result = await read_through_transport(
            read_artifact,
            artifact.id,
            latest_content.id,
            reader,
        )
        assert old_result == ResourceResult(
            contents=[ResourceContent(b"old bytes", mime_type="application/pdf")]
        )
        assert latest_result == ResourceResult(
            contents=[ResourceContent(b"latest bytes", mime_type="application/pdf")]
        )
        await mcp_probe.drain_usage()
        assert await usage_events() == [
            ("artifact_read", len(b"old bytes"), [owner], [owner], owner),
            ("artifact_read", len(b"latest bytes"), [owner], [owner], owner),
        ]

        with pytest.raises(ResourceError, match="not visible or does not exist"):
            await read_through_transport(
                read_artifact,
                uuid7(),
                latest_content.id,
                reader,
            )

        with pytest.raises(ResourceError, match="not visible or does not exist"):
            await read_through_transport(
                read_artifact,
                artifact.id,
                latest_content.id,
                stranger,
            )

        corrupt = Blob(
            content_hash=sql.uuid8(b"different bytes"),
            size=latest.size,
            stored_size=latest.stored_size,
            storage_key=latest.key + "-corrupt",
            media_type="application/pdf",
        )
        corrupt_stored = await store.put(b"corrupt bytes")
        corrupt.storage_key = corrupt_stored.key
        corrupt.size = corrupt_stored.size
        corrupt.stored_size = corrupt_stored.stored_size
        corrupt.encoding = corrupt_stored.encoding
        async with User.private(owner) as session:
            session.add(corrupt)
            await session.flush()
            corrupt_content = artifact_content(
                artifact.id,
                corrupt.id,
                owner,
                [owner],
                revision=3,
                state=Artifact.Content.State.ready,
            )
            session.add(corrupt_content)

        with pytest.raises(ResourceError, match="failed integrity verification"):
            await read_through_transport(
                read_artifact,
                artifact.id,
                corrupt_content.id,
                reader,
            )

        async def unavailable_get(*_args: object, **_kwargs: object) -> bytes:
            raise ObjectStoreError("offline")

        unavailable_store = cast("ByteStore", SimpleNamespace(get=unavailable_get))
        unavailable_server = mcp_probe.build_server(store=unavailable_store)
        unavailable_reader = unavailable_server.artifact_resource()
        with pytest.raises(ResourceError, match="object storage is temporarily unavailable"):
            await read_through_transport(
                unavailable_reader,
                artifact.id,
                latest_content.id,
                reader,
            )

        # Visibility, integrity, and storage availability failures never account a read.
        await mcp_probe.drain_usage()
        assert len(await usage_events()) == 2

    dbutil.run(body())
