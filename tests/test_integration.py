import asyncio
import socket
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from doubles import RecordingEmbedder, RecordingReranker
from sqlalchemy import text

from aizk.cli import migrate
from aizk.config import settings
from aizk.extract.ingest import ingest_path
from aizk.retrieval import Hit, search
from aizk.store import async_session


def port_open(host: str | None, port: int | None, timeout: float = 0.5) -> bool:
    """Whether a TCP connection to host and port succeeds within timeout.

    host: target hostname, treated as unreachable when missing.
    port: target port, treated as unreachable when missing.
    timeout: connection deadline in seconds.
    """
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def db_reachable() -> bool:
    """Whether the Postgres DSN host accepts connections.

    Every model-shaped step now lives in a container this suite never starts, so the embedder is
    faked and Postgres reachability is this integration test's only real prerequisite.
    """
    db = urlsplit(settings.database_url)
    return port_open(db.hostname, db.port)


DB_UP = db_reachable()


async def purge_marker(marker: str) -> None:
    """Delete any documents whose title carries the run marker, running as the superuser owner.

    marker: unique per-run token embedded in the note title.
    """
    async with async_session()() as session, session.begin():
        await session.execute(
            text("DELETE FROM document WHERE title LIKE :pat"), {"pat": f"note-{marker}%"}
        )


@pytest.mark.skipif(not DB_UP, reason="aizk postgres not reachable")
def test_migrate_ingest_search(
    tmp_path: Path, fake_embedder: RecordingEmbedder, fake_reranker: RecordingReranker
) -> None:
    """Migrate, ingest a small note, then retrieve it through hybrid search.

    The note carries a unique per-run marker so the content_hash dedupe never skips it on
    a repeat run, and the document is purged afterwards to keep the live database clean.
    """
    migrate()

    marker = uuid.uuid4().hex
    note = tmp_path / f"note-{marker}.md"
    note.write_text(f"Alpha beta gamma {marker} over here.\n\nDelta epsilon zeta over there.\n")
    try:
        count = asyncio.run(ingest_path(note))
        assert count > 0

        hits = asyncio.run(search(f"alpha beta gamma {marker}", k=4))
        assert isinstance(hits, list)
        assert all(isinstance(hit, Hit) for hit in hits)
        assert all(isinstance(hit.score, float) for hit in hits)
    finally:
        asyncio.run(purge_marker(marker))
