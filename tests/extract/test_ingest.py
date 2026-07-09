import hashlib
import uuid
from pathlib import Path

import dbutil
import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import text as sql

from aizk.config import Settings
from aizk.extract.ingest import content_hash, contextual_lexical, ingest_path, ingest_text
from aizk.store import acting_as


@given(text=st.text())
def test_content_hash_is_the_stable_sha256_of_the_utf8_bytes(text: str) -> None:
    """The digest is the sha256 hex of the utf-8 bytes and repeats for identical content."""
    digest = content_hash(text)
    assert digest == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert content_hash(text) == digest


def test_contextual_lexical_prepends_the_title_when_enabled(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With contextual bm25 on and a title present the lexical lane text is title-prefixed."""
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
    """The lexical preamble is dropped when the flag is off or the stripped title is blank."""
    monkeypatch.setattr(settings, "contextual_bm25", bm25)
    assert contextual_lexical(title, "body span") is None


@pytest.mark.usefixtures("migrated_db", "fake_embedder")
def test_ingest_text_dedupes_on_content_hash(settings: Settings) -> None:
    """Remembering the same text twice lands one document and returns its id both times."""
    title = f"note {uuid.uuid4().hex}"

    async def body() -> tuple[uuid.UUID | None, uuid.UUID | None, int]:
        await dbutil.reset_db()
        await dbutil.seed_user(settings.system_user_id, is_admin=True)
        note = "a remembered note about the bi-temporal memory spine across time"
        first = await ingest_text(note, title=title)
        second = await ingest_text(note, title=title)
        async with acting_as(settings.system_user_id) as session:
            count = await session.scalar(
                sql("SELECT count(*) FROM document WHERE title = :t"), {"t": title}
            )
        return first, second, count

    first, second, count = dbutil.run(body())
    assert isinstance(first, uuid.UUID)
    assert first == second
    assert count == 1


@pytest.mark.usefixtures("migrated_db", "fake_embedder")
def test_ingest_path_routes_each_lane_dedupes_and_skips_the_rest(
    settings: Settings, tmp_path: Path
) -> None:
    """A directory ingest stores one document per chunked file, routed to its lane, then dedupes.

    Python routes through the code chunker to a `code` document and markdown through the prose one
    to a `note`, the empty note yields no chunks so it is skipped without a document, the pdf sits
    outside the text filter so the walk never reads it, and a re-ingest of the same content stores
    none.
    """
    (tmp_path / "note.md").write_text(
        "# note\n\nthe spine remembers facts across time.\n", encoding="utf-8"
    )
    (tmp_path / "code.py").write_text("def fn(value):\n    return value + 1\n", encoding="utf-8")
    (tmp_path / "empty.md").write_text("", encoding="utf-8")
    (tmp_path / "scan.pdf").write_bytes(b"%PDF-1.4 not really a pdf")

    async def body() -> tuple[int, int, list[str]]:
        await dbutil.reset_db()
        await dbutil.seed_user(settings.system_user_id, is_admin=True)
        first = await ingest_path(tmp_path)
        again = await ingest_path(tmp_path)
        async with acting_as(settings.system_user_id) as session:
            rows = await session.execute(
                sql("SELECT kind FROM document WHERE source_uri LIKE :pat ORDER BY kind"),
                {"pat": f"%{tmp_path.name}%"},
            )
            kinds = list(rows.scalars().all())
        return first, again, kinds

    first, again, kinds = dbutil.run(body())
    assert first == 2
    assert again == 0
    assert kinds == ["code", "note"]


@pytest.mark.usefixtures("migrated_db", "fake_embedder")
def test_reingesting_a_changed_file_refreshes_its_standing_document(
    settings: Settings, tmp_path: Path
) -> None:
    """An edited file keeps its document row and swaps the content under it, never a crash.

    ``source_uri`` is the document's stable identity, so the changed content must land as an
    in-place refresh: same document id, the new content hash and title-stable row, the old
    chunks replaced by the fresh spans with ``processed_at`` null so the graph re-extracts,
    and the write counted. A second ingest of the unchanged edit then dedupes to nothing.
    The pre-fix behavior raised ``UniqueViolationError`` on ``document_source_uri_key``, which
    is exactly the edited-Zettel re-ingest case the vault migration hit.
    """
    note = tmp_path / "note.md"
    note.write_text("# note\n\nthe original status line before the edit.\n", encoding="utf-8")

    async def body() -> tuple[int, int, int, list, list]:
        await dbutil.reset_db()
        await dbutil.seed_user(settings.system_user_id, is_admin=True)
        first = await ingest_path(tmp_path)
        note.write_text("# note\n\nthe REWRITTEN status line after the edit.\n", encoding="utf-8")
        changed = await ingest_path(tmp_path)
        unchanged = await ingest_path(tmp_path)
        async with acting_as(settings.system_user_id) as session:
            docs = list(
                (
                    await session.execute(
                        sql("SELECT id, content_hash FROM document WHERE source_uri LIKE :pat"),
                        {"pat": f"%{tmp_path.name}%"},
                    )
                ).all()
            )
            chunks = list(
                (
                    await session.execute(
                        sql(
                            "SELECT text, processed_at FROM chunk WHERE document_id = :d "
                            "ORDER BY ord"
                        ),
                        {"d": docs[0][0]},
                    )
                ).all()
            )
        return first, changed, unchanged, docs, chunks

    first, changed, unchanged, docs, chunks = dbutil.run(body())
    assert first == 1
    assert changed == 1  # the refresh counts as written
    assert unchanged == 0  # and the refreshed content then dedupes
    assert len(docs) == 1  # one standing document, never a duplicate row
    assert docs[0][1] == content_hash(note.read_text(encoding="utf-8"))
    assert all("REWRITTEN" in text for text, _ in chunks)  # old spans fully replaced
    assert all(processed_at is None for _, processed_at in chunks)  # re-extraction pending
