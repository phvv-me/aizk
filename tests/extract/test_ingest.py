import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from aizk.cli import migrate
from aizk.config import settings
from aizk.extract.ingest import (
    ingest_image,
    ingest_path,
    ingest_text,
    is_text,
    record_reference,
)
from aizk.store import acting_as


async def purge(marker: str) -> None:
    """Delete as the system owner every document this test created, keeping the live db clean.

    marker: the per-run token embedded in each created title or uri.
    """
    async with acting_as(settings.system_principal_id) as session:
        await session.execute(
            text("DELETE FROM document WHERE title LIKE :pat OR source_uri LIKE :pat"),
            {"pat": f"%{marker}%"},
        )


async def count_documents(marker: str) -> int:
    """Count the documents this test created, the rows a marker matches.

    marker: the per-run token embedded in each created title or uri.
    """
    async with acting_as(settings.system_principal_id) as session:
        return await session.scalar(
            text("SELECT count(*) FROM document WHERE title LIKE :pat"), {"pat": f"%{marker}%"}
        )


@pytest.fixture
def migrated_db(requires_db: None) -> None:
    """Ensure the live schema exists before a DB-integration ingest, skipping with no Postgres.

    requires_db: the gate that skips the test when the Postgres DSN host is unreachable.
    """
    migrate()


@pytest.fixture
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn rerank off for the duration of one test, alongside the fake_embedder fixture.

    The ingest lane never reranks, and the fake embedder returns vectors at the configured
    embed_dim width the halfvec column accepts, so a write lands under it with no model or network.
    """
    monkeypatch.setattr(settings, "rerank", False)


@pytest.mark.usefixtures("migrated_db", "fake_embedder", "fake_settings")
def test_ingest_text_dedupes_on_content() -> None:
    """Remembering the same text twice lands one document and returns its id both times."""
    marker = uuid.uuid4().hex
    body = f"a remembered note {marker} about the bi-temporal memory spine"
    title = f"note {marker}"
    try:
        first = asyncio.run(ingest_text(body, title=title))
        second = asyncio.run(ingest_text(body, title=title))
        assert isinstance(first, uuid.UUID)
        assert first == second
        assert asyncio.run(count_documents(marker)) == 1
    finally:
        asyncio.run(purge(marker))


def test_ingest_path_admits_only_text_and_code() -> None:
    """The directory walk admits text and code paths only, no markup or binary document format."""
    assert is_text(Path("note.md"))
    assert is_text(Path("module.py"))
    assert not is_text(Path("scan.pdf"))
    assert not is_text(Path("report.docx"))


@pytest.mark.usefixtures("migrated_db", "fake_embedder", "fake_settings")
def test_ingest_path_walks_each_lane_dedupes_and_skips_binary_files(tmp_path: Path) -> None:
    """A directory ingest stores one document per chunked text file, skipping the rest.

    The empty note produces no chunks, so the walk skips it without a document, the pdf carries a
    suffix outside TEXT_SUFFIXES so the walk never even reads it, aizk leaving that to the coding
    agent that already parses its own documents, while the prose and code files each land one, and
    a re-ingest of the same content stores none.
    """
    marker = uuid.uuid4().hex
    (tmp_path / f"{marker}-note.md").write_text(
        f"# {marker} note\n\nthe spine remembers facts across time.\n", encoding="utf-8"
    )
    (tmp_path / f"{marker}-code.py").write_text(
        f"def {marker.replace('-', '_')[:8]}_fn(value):\n    return value + 1\n", encoding="utf-8"
    )
    (tmp_path / f"{marker}-empty.md").write_text("", encoding="utf-8")
    (tmp_path / f"{marker}-scan.pdf").write_bytes(b"%PDF-1.4 not really a pdf " + marker.encode())
    try:
        ingested = asyncio.run(ingest_path(tmp_path))
        assert ingested == 2
        assert asyncio.run(ingest_path(tmp_path)) == 0
        assert asyncio.run(count_documents(marker)) == 2
    finally:
        asyncio.run(purge(marker))


def test_record_reference_dedupes_on_uri(migrated_db: None) -> None:
    """Recording the same uri twice lands one chunkless reference and returns its id both times."""
    marker = uuid.uuid4().hex
    uri = f"https://example.test/{marker}"
    try:
        first = asyncio.run(record_reference(uri))
        second = asyncio.run(record_reference(uri))
        assert isinstance(first, uuid.UUID)
        assert first == second
    finally:
        asyncio.run(purge(marker))


@pytest.mark.usefixtures("migrated_db", "fake_embedder", "fake_settings")
def test_ingest_image_dedupes_and_stores_one_chunk(tmp_path: Path) -> None:
    """An image embeds through the multimodal lane into one captioned chunk, deduped on bytes."""
    marker = uuid.uuid4().hex
    image = tmp_path / f"{marker}.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + marker.encode("ascii"))
    caption = f"a photo marked {marker}"
    try:
        first = asyncio.run(ingest_image(image, caption=caption))
        second = asyncio.run(ingest_image(image, caption=caption))
        assert isinstance(first, uuid.UUID)
        assert first == second

        async def stored() -> tuple[str, int]:
            async with acting_as(settings.system_principal_id) as session:
                kind = await session.scalar(
                    text("SELECT kind FROM document WHERE id = :i"), {"i": first}
                )
                chunks = await session.scalar(
                    text("SELECT count(*) FROM chunk WHERE document_id = :i AND text = :t"),
                    {"i": first, "t": caption},
                )
                return kind, chunks

        kind, chunks = asyncio.run(stored())
        assert kind == "image"
        assert chunks == 1
    finally:
        asyncio.run(purge(marker))
