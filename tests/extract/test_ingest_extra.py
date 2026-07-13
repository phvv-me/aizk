import uuid
from pathlib import Path

import dbutil
import pytest
from doubles import RecordingEmbedder
from sqlalchemy import text

from aizk.config import settings
from aizk.extract.ingest import ingest_image, record_reference, remember_session
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")


async def seed_system() -> uuid.UUID:
    await dbutil.reset_db()
    return settings.system_user_id


async def count(table: str, where: str, params: dict[str, object]) -> int:
    async with dbutil.admin_engine().connect() as connection:
        row = await connection.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params)
        return int(row.scalar_one())


def test_record_reference_dedupes_on_uri(fake_embedder: RecordingEmbedder) -> None:
    async def body() -> None:
        await seed_system()
        first = await record_reference(User.system(), "https://arxiv.org/abs/1", title="paper")
        again = await record_reference(User.system(), "https://arxiv.org/abs/1")
        assert first == again
        assert (
            await count(
                "document",
                "kind = 'reference' AND source_uri = :u",
                {"u": "https://arxiv.org/abs/1"},
            )
            == 1
        )
        assert (
            await count(
                "chunk",
                "document_id = :id AND embedding IS NOT NULL AND processed_at IS NOT NULL",
                {"id": first},
            )
            == 1
        )

    dbutil.run(body())


def test_remember_session_writes_one_embedded_working_item(
    fake_embedder: RecordingEmbedder,
) -> None:
    async def body() -> None:
        owner = await seed_system()
        item_id = await remember_session(User.system(), "a captured thought", kind="note")
        assert (
            await count(
                "session_item",
                "id = :id AND created_by = :o AND embedding IS NOT NULL",
                {"id": item_id, "o": owner},
            )
            == 1
        )
        assert fake_embedder.calls  # the text lane was exercised

    dbutil.run(body())


def test_ingest_image_stores_and_dedupes_on_bytes(
    fake_embedder: RecordingEmbedder, tmp_path: Path
) -> None:
    async def body() -> None:
        await seed_system()
        picture = tmp_path / "shot.png"
        picture.write_bytes(b"\x89PNG\r\n\x1a\n fake bytes")
        first = await ingest_image(User.system(), picture, caption="a diagram")
        again = await ingest_image(User.system(), picture)
        assert first == again
        assert await count("document", "id = :id AND kind = 'image'", {"id": first}) == 1
        assert (
            await count("chunk", "document_id = :id AND embedding IS NOT NULL", {"id": first}) == 1
        )
        assert fake_embedder.image_calls  # the image lane was exercised

    dbutil.run(body())
