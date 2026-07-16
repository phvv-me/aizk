import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import dbutil
import pytest
from doubles import RecordingEmbedder
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5
from pydantic import UUID5, UUID7, UUID8
from sqlalchemy import text as sql
from sqlmodel import select

from aizk.config import Settings
from aizk.extract.ingest import (
    DocumentStore,
    TextSource,
    contextual_lexical,
    ingest_path,
    ingest_text,
    ingest_texts,
)
from aizk.provenance import CaptureContext
from aizk.store import (
    Chunk,
    Document,
    Entity,
    Fact,
)
from aizk.store.identity import User


def expected_digest(text: str) -> UUID8:
    value = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:16])
    return uuid.UUID(int=(value & ~(0xF << 76) & ~(0x3 << 62)) | (8 << 76) | (0x2 << 62))


@pytest.mark.usefixtures("migrated_db")
@given(text=st.text())
def test_document_store_hashes_utf8_text_with_postgres_pgcrypto(text: str) -> None:
    async def body() -> tuple[list[UUID8], list[UUID8]]:
        async with User.system() as session:
            store = DocumentStore(session)
            return await store.hash_texts([text, text]), await store.hash_texts([])

    digests, empty = dbutil.run(body())
    first, second = digests
    assert first == expected_digest(text)
    assert second == first
    assert empty == []


def test_contextual_lexical_prepends_the_title_when_enabled(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "contextual_bm25", True)
    assert contextual_lexical("Title", "body span") == "Title\nbody span"


@pytest.mark.parametrize(
    ("bm25", "title"),
    [(False, "Title"), (True, "   "), (False, "")],
    ids=["flag-off", "blank-title", "off-and-blank"],
)
def test_contextual_lexical_is_null_when_off_or_titleless(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, bm25: bool, title: str
) -> None:
    monkeypatch.setattr(settings, "contextual_bm25", bm25)
    assert contextual_lexical(title, "body span") is None


@pytest.mark.usefixtures("migrated_db", "fake_embedder")
def test_ingest_texts_batches_stores_and_skips_the_blank_source(settings: Settings) -> None:
    real = "a batched note carrying enough prose to become one embedded chunk of memory"

    async def body() -> list[UUID5 | UUID7 | None]:
        await dbutil.reset_db()
        blank = await ingest_texts(User.system(), [TextSource(text="   ")])
        return [*blank, *(await ingest_texts(User.system(), [TextSource(text=real)]))]

    results = dbutil.run(body())
    assert results[0] is None  # the blank source plans nothing and stores no document
    assert isinstance(results[1], uuid.UUID)  # the real source becomes one stored document


@pytest.mark.usefixtures("migrated_db")
def test_ingest_text_dedupes_before_embedding(
    settings: Settings, fake_embedder: RecordingEmbedder
) -> None:
    title = f"note {uuid5().hex}"

    async def body() -> tuple[UUID5 | UUID7 | None, UUID5 | UUID7 | None, int, int, int]:
        await dbutil.reset_db()
        note = "a remembered note about the bi-temporal memory spine across time"
        first = await ingest_text(User.system(), note, title=title)
        calls_after_first = len(fake_embedder.calls)
        second = await ingest_text(User.system(), note, title=title)
        async with dbutil.actor(settings.system_user_id) as session:
            count = (
                await session.exec(
                    sql("SELECT count(*) FROM document WHERE title = :t"),
                    params={"t": title},
                )
            ).scalar_one()
        return first, second, count, calls_after_first, len(fake_embedder.calls)

    first, second, count, calls_after_first, calls_after_second = dbutil.run(body())
    assert isinstance(first, uuid.UUID)
    assert first == second
    assert count == 1
    assert calls_after_first == calls_after_second == 1


@pytest.mark.usefixtures("migrated_db", "fake_embedder")
def test_ingest_text_uses_source_uri_as_message_identity(settings: Settings) -> None:
    shared = "the same short group message has enough content to become one prose chunk"
    corrected = f"{shared} after correction"

    async def body() -> tuple[
        UUID5 | UUID7 | None, UUID5 | UUID7 | None, UUID5 | UUID7 | None, list[UUID8]
    ]:
        await dbutil.reset_db()
        first = await ingest_text(User.system(), shared, source_uri="groupmem://room/msg-1")
        second = await ingest_text(User.system(), shared, source_uri="groupmem://room/msg-2")
        refreshed = await ingest_text(User.system(), corrected, source_uri="groupmem://room/msg-1")
        async with dbutil.actor(settings.system_user_id) as session:
            rows = await session.exec(select(Document).order_by(Document.source_uri))
            hashes = [document.content_hash for document in rows]
        return first, second, refreshed, hashes

    first, second, refreshed, hashes = dbutil.run(body())
    assert first != second
    assert refreshed == first
    assert hashes == [expected_digest(corrected), expected_digest(shared)]


@pytest.mark.usefixtures("migrated_db", "fake_embedder")
def test_ingest_text_refreshes_changed_retrieval_metadata(settings: Settings) -> None:
    observed = datetime(2026, 7, 15, tzinfo=UTC)

    async def body() -> tuple[
        UUID7 | None,
        UUID7 | None,
        str,
        str,
        datetime | None,
    ]:
        await dbutil.reset_db()
        original = await ingest_text(
            User.system(),
            "# Old title\n- Type Area\nAn older source body.",
            source_uri="vault:///Zettelkasten/Definitions.md",
        )
        refreshed = await ingest_text(
            User.system(),
            (
                "# Definitions\n- Type Project\n- part_of [Area] Productivity\n"
                "A corrected source body."
            ),
            source_uri="vault:///Zettelkasten/Definitions.md",
            capture=CaptureContext(observed_at=observed),
        )
        async with dbutil.actor(settings.system_user_id) as session:
            document = (await session.exec(select(Document))).one()
        return (
            original,
            refreshed,
            document.title,
            document.subject_type,
            document.observed_at,
        )

    original, refreshed, title, subject_type, observed_at = dbutil.run(body())
    assert refreshed == original
    assert (title, subject_type, observed_at) == (
        "Definitions",
        "project",
        observed,
    )


@pytest.mark.usefixtures("migrated_db")
def test_ingest_text_updates_validity_without_reembedding(
    settings: Settings,
    fake_embedder: RecordingEmbedder,
) -> None:
    text = "# Aizk\n- Type Project\n- has_status [Status] Active\nThe brief stays stable."
    observed = datetime(2026, 7, 15, tzinfo=UTC)

    async def body() -> tuple[UUID7 | None, UUID7 | None, Document]:
        await dbutil.reset_db()
        original = await ingest_text(
            User.system(),
            text,
            source_uri="vault:///Zettelkasten/Aizk.md",
        )
        calls = len(fake_embedder.calls)
        refreshed = await ingest_text(
            User.system(),
            text,
            source_uri="vault:///Zettelkasten/Aizk.md",
            capture=CaptureContext(observed_at=observed),
        )
        assert len(fake_embedder.calls) == calls
        async with dbutil.actor(settings.system_user_id) as session:
            document = (await session.exec(select(Document))).one()
        return original, refreshed, document

    original, refreshed, document = dbutil.run(body())

    assert refreshed == original
    assert document.subject_type == "project"
    assert document.observed_at == observed


@pytest.mark.usefixtures("migrated_db", "fake_embedder")
def test_managed_brief_title_survives_a_changed_source_uri(
    settings: Settings, fake_embedder: RecordingEmbedder
) -> None:
    text = "# Research\n- Type Area\nThe canonical brief stays one document."

    async def body() -> tuple[UUID7 | None, UUID7 | None, list[Document]]:
        await dbutil.reset_db()
        original = await ingest_text(
            User.system(),
            text,
            source_uri="vault/Zettelkasten/Research.md",
        )
        calls = len(fake_embedder.calls)
        refreshed = await ingest_text(
            User.system(),
            text,
            source_uri="vault:///Zettelkasten/Research.md",
        )
        assert len(fake_embedder.calls) == calls
        async with dbutil.actor(settings.system_user_id) as session:
            documents = list(await session.exec(select(Document)))
        return original, refreshed, documents

    original, refreshed, documents = dbutil.run(body())

    assert refreshed == original
    assert len(documents) == 1
    assert documents[0].source_uri == "vault:///Zettelkasten/Research.md"


@pytest.mark.usefixtures("migrated_db", "fake_embedder")
def test_refresh_retracts_claims_mined_from_removed_source_text(settings: Settings) -> None:
    async def body() -> tuple[bool, dict, UUID5 | UUID7 | None]:
        await dbutil.reset_db()
        document_id = await ingest_text(
            User.system(),
            "The retired plan required a paper approval before release.",
            source_uri="groupmem://room/edited-message",
        )
        assert document_id is not None
        async with dbutil.actor(settings.system_user_id) as session:
            chunk_id = (
                await session.exec(select(Chunk.id).where(Chunk.document_id == document_id))
            ).one()
            entity_id = uuid5()
            content_id = uuid5()
            session.add(Entity.Content(id=entity_id, name="retired plan", type="concept"))
            await session.flush()
            session.add(
                Entity.Claim(
                    content_id=entity_id,
                    created_by=settings.system_user_id,
                    scopes=[settings.system_user_id],
                )
            )
            session.add(
                Fact.Content(
                    id=content_id,
                    subject_id=entity_id,
                    predicate="related_to",
                    statement="The retired plan required paper approval.",
                )
            )
            await session.flush()
            claim = Fact.Claim(
                content_id=content_id,
                created_by=settings.system_user_id,
                scopes=[settings.system_user_id],
                source_chunk_id=chunk_id,
            )
            session.add(claim)
            await session.flush()
            claim_id = claim.id
        await ingest_text(
            User.system(),
            "The replacement note contains no approval requirement at all.",
            source_uri="groupmem://room/edited-message",
        )
        async with dbutil.actor(settings.system_user_id) as session:
            historical = (
                await session.exec(
                    select(Fact.Claim)
                    .where(Fact.Claim.id == claim_id)
                    .execution_options(**{settings.skip_live_gate: True})
                )
            ).one()
        return (
            historical.recorded.upper is not None,
            historical.attributes,
            historical.source_chunk_id,
        )

    closed, attributes, source_chunk_id = dbutil.run(body())
    assert closed and "source_refreshed" in attributes
    assert source_chunk_id is None


@pytest.mark.usefixtures("migrated_db", "fake_embedder")
def test_ingest_path_routes_each_lane_dedupes_and_skips_the_rest(
    settings: Settings, tmp_path: Path
) -> None:
    (tmp_path / "note.md").write_text(
        "# note\n\nthe spine remembers facts across time.\n", encoding="utf-8"
    )
    (tmp_path / "code.py").write_text("def fn(value):\n    return value + 1\n", encoding="utf-8")
    (tmp_path / "empty.md").write_text("", encoding="utf-8")
    (tmp_path / "scan.pdf").write_bytes(b"%PDF-1.4 not really a pdf")

    async def body() -> tuple[int, int, list[str]]:
        await dbutil.reset_db()
        first = await ingest_path(User.system(), tmp_path)
        again = await ingest_path(User.system(), tmp_path)
        async with dbutil.actor(settings.system_user_id) as session:
            rows = await session.exec(
                sql("SELECT title FROM document WHERE source_uri LIKE :pat ORDER BY title"),
                params={"pat": f"%{tmp_path.name}%"},
            )
            titles = list(rows.scalars().all())
        return first, again, titles

    first, again, titles = dbutil.run(body())
    assert first == 2
    assert again == 0
    assert titles == ["code", "note"]


@pytest.mark.usefixtures("migrated_db", "fake_embedder")
def test_reingesting_a_changed_file_refreshes_its_standing_document(
    settings: Settings, tmp_path: Path
) -> None:
    note = tmp_path / "note.md"
    note.write_text("# note\n\nthe original status line before the edit.\n", encoding="utf-8")

    async def body() -> tuple[int, int, int, list, list]:
        await dbutil.reset_db()
        first = await ingest_path(User.system(), tmp_path)
        note.write_text("# note\n\nthe REWRITTEN status line after the edit.\n", encoding="utf-8")
        changed = await ingest_path(User.system(), tmp_path)
        unchanged = await ingest_path(User.system(), tmp_path)
        async with dbutil.actor(settings.system_user_id) as session:
            docs = list(
                (
                    await session.exec(
                        sql("SELECT id, content_hash FROM document WHERE source_uri LIKE :pat"),
                        params={"pat": f"%{tmp_path.name}%"},
                    )
                ).all()
            )
            chunks = list(
                (
                    await session.exec(
                        sql(
                            "SELECT text, processed_at FROM chunk WHERE document_id = :d "
                            "ORDER BY ord"
                        ),
                        params={"d": docs[0][0]},
                    )
                ).all()
            )
        return first, changed, unchanged, docs, chunks

    first, changed, unchanged, docs, chunks = dbutil.run(body())
    assert first == 1
    assert changed == 1  # the refresh counts as written
    assert unchanged == 0  # and the refreshed content then dedupes
    assert len(docs) == 1  # one standing document, never a duplicate row
    assert docs[0][1] == expected_digest(note.read_text(encoding="utf-8"))
    assert all("REWRITTEN" in text for text, _ in chunks)  # old spans fully replaced
    assert all(processed_at is None for _, processed_at in chunks)  # re-extraction pending
