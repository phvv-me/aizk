import dbutil
import pytest
from doubles import RecordingEmbedder
from pydantic import UUID5, UUID7
from sqlalchemy import text

from aizk.config import settings
from aizk.extract.ingest import remember_session
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")


async def seed_system() -> UUID5 | UUID7:
    await dbutil.reset_db()
    return settings.system_user_id


async def count(table: str, where: str, params: dict[str, UUID5 | UUID7]) -> int:
    async with dbutil.admin_engine().connect() as connection:
        row = await connection.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params)
        return int(row.scalar_one())


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
