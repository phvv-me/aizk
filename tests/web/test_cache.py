from datetime import UTC, datetime, timedelta
from typing import cast

import dbutil
import pytest
from id_factory import uuid5
from pydantic import UUID5
from pydantic.networks import AnyHttpUrl
from sqlmodel import select, update
from web_doubles import ScriptedScanner, as_scanner

from aizk.config import settings
from aizk.graph.build import pending_chunks
from aizk.integrations.clamav import MalwareRejectedError, MalwareUnavailableError
from aizk.integrations.web import Freshness
from aizk.ontology import System
from aizk.store import Chunk, Document
from aizk.store.identity import User
from aizk.types import Scopes
from aizk.web import WebCache, WebFinding

pytestmark = pytest.mark.usefixtures("migrated_db")


def finding(
    url: str = "https://example.test/page",
    text: str = "the public page body",
    persistable: bool = True,
) -> WebFinding:
    """One page this call read, ready to hand the cache."""
    return WebFinding(
        url=cast("AnyHttpUrl", url),
        text=text,
        provider="firecrawl-reader",
        retrieved_at=datetime.now(UTC),
        title="A public page",
        persistable=persistable,
    )


def cache(error: Exception | None = None) -> tuple[WebCache, ScriptedScanner]:
    """A cache over a scanner double, with the scanner returned so a test can read it."""
    scanner = ScriptedScanner(error)
    return WebCache.build(settings, as_scanner(scanner)), scanner


@pytest.mark.parametrize(
    ("freshness", "days"),
    [
        (Freshness.stable, settings.web_search_stable_days),
        (Freshness.dated, settings.web_search_dated_days),
        (Freshness.volatile, settings.web_search_volatile_days),
    ],
)
def test_each_freshness_bucket_expires_a_page_on_its_own_schedule(
    freshness: Freshness, days: int
) -> None:
    written, _ = cache()

    horizon = written.expiry(freshness) - datetime.now(UTC)

    assert timedelta(days=days) - timedelta(minutes=1) < horizon <= timedelta(days=days)


def test_a_cached_page_becomes_an_expiring_web_origin_document() -> None:
    owner = uuid5()
    user = User.private(owner)
    written, scanner = cache()

    async def body() -> tuple[Document, list[Chunk]]:
        kept = await written.keep(user, (finding(),), Freshness.dated, frozenset({owner}))
        assert len(kept) == 1
        async with user as session:
            document = (
                await session.exec(
                    select(Document).where(
                        Document.source_uri == Document.cache_locator("https://example.test/page")
                    )
                )
            ).one()
            chunks = list(
                await session.exec(select(Chunk).where(Chunk.document_id == document.id))
            )
        return document, chunks

    document, chunks = dbutil.run(body())

    assert document.origin is Document.Origin.web_cache
    assert document.title == "A public page"
    # the stored locator is namespaced, so it can never occupy an authored source's slot
    assert document.source_uri == "web-cache:https://example.test/page"
    assert Document.public_url(document.source_uri) == "https://example.test/page"
    assert document.subject_type is None
    assert document.expires_at is not None
    assert scanner.scanned == [b"the public page body"]
    # the page is still ordinary memory, so retrieval reaches it exactly as it reaches a note
    assert chunks


def test_a_cached_page_is_never_offered_to_the_graph_or_the_passes_behind_it() -> None:
    owner = uuid5()
    user = User.private(owner)
    scopes = frozenset({owner})
    written, _ = cache()

    async def body() -> tuple[list[Chunk], list[Chunk]]:
        await written.keep(user, (finding(),), Freshness.stable, scopes)
        cached = await pending_chunks(scopes, None, None)
        async with user as session:
            session.add(
                Document(
                    title="An authored note",
                    content_hash=uuid5(),
                    created_by=owner,
                    scopes=[owner],
                    chunks=[Chunk(ord=0, text="a note", created_by=owner, scopes=[owner])],
                )
            )
        return cached, await pending_chunks(scopes, None, None)

    cached, after_a_note = dbutil.run(body())

    # nothing the cache wrote is ever pending projection, while an ordinary note still is
    assert cached == []
    assert len(after_a_note) == 1
    assert after_a_note[0].text == "a note"


def test_a_page_no_provider_licensed_is_answered_with_and_never_written() -> None:
    owner = uuid5()
    user = User.private(owner)
    written, scanner = cache()

    async def body() -> tuple[tuple[WebFinding, ...], list[Document]]:
        kept = await written.keep(
            user,
            (finding(url="https://vendor.test/row", persistable=False),),
            Freshness.stable,
            frozenset({owner}),
        )
        async with user as session:
            stored = list(
                await session.exec(
                    select(Document).where(
                        Document.source_uri == Document.cache_locator("https://vendor.test/row")
                    )
                )
            )
        return kept, stored

    kept, stored = dbutil.run(body())

    assert len(kept) == 1
    assert stored == []
    assert scanner.scanned == []


def test_a_page_the_scanner_rejects_is_dropped_from_the_answer_as_well() -> None:
    owner = uuid5()
    user = User.private(owner)
    written, _ = cache(MalwareRejectedError("Eicar-Test-Signature"))

    async def body() -> tuple[tuple[WebFinding, ...], list[Document]]:
        kept = await written.keep(
            user,
            (finding(url="https://malicious.test/page"),),
            Freshness.stable,
            frozenset({owner}),
        )
        async with user as session:
            stored = list(
                await session.exec(
                    select(Document).where(
                        Document.source_uri
                        == Document.cache_locator("https://malicious.test/page")
                    )
                )
            )
        return kept, stored

    kept, stored = dbutil.run(body())

    assert kept == ()
    assert stored == []


def test_a_page_the_scanner_could_not_judge_is_answered_with_but_not_stored() -> None:
    owner = uuid5()
    user = User.private(owner)
    written, _ = cache(MalwareUnavailableError("ClamAV is unavailable"))

    async def body() -> tuple[tuple[WebFinding, ...], list[Document]]:
        kept = await written.keep(
            user,
            (finding(url="https://unscanned.test/page"),),
            Freshness.stable,
            frozenset({owner}),
        )
        async with user as session:
            stored = list(
                await session.exec(
                    select(Document).where(
                        Document.source_uri
                        == Document.cache_locator("https://unscanned.test/page")
                    )
                )
            )
        return kept, stored

    kept, stored = dbutil.run(body())

    assert len(kept) == 1
    assert stored == []


def test_refetching_one_page_revises_the_document_it_already_wrote() -> None:
    owner = uuid5()
    user = User.private(owner)
    written, _ = cache()
    url = "https://example.test/revised"

    async def body() -> list[Document]:
        await written.keep(
            user, (finding(url=url, text="first read"),), Freshness.dated, frozenset({owner})
        )
        await written.keep(
            user, (finding(url=url, text="second read"),), Freshness.dated, frozenset({owner})
        )
        async with user as session:
            cached = select(Document).where(Document.source_uri == Document.cache_locator(url))
            return list(await session.exec(cached))

    stored = dbutil.run(body())

    assert len(stored) == 1
    assert stored[0].origin is Document.Origin.web_cache


def authored_note(owner: UUID5, title: str, subject_type: str | None = None) -> Document:
    """One note the caller wrote, of the shape a crafted page would try to impersonate."""
    return Document(
        title=title,
        subject_type=subject_type,
        content_hash=uuid5(),
        created_by=owner,
        scopes=[owner],
        chunks=[
            Chunk(ord=0, text="what the owner actually wrote", created_by=owner, scopes=[owner])
        ],
    )


def test_a_crafted_page_cannot_impersonate_and_overwrite_an_authored_note() -> None:
    """The H1 plus `- Type` collision, which used to refresh the note it named."""
    owner = uuid5()
    user = User.private(owner)
    written, _ = cache()
    crafted = "# Compression target ladder\n- Type Concept\n\nwhatever the attacker wants"

    async def body() -> tuple[Document, list[Document]]:
        async with user as session:
            session.add(authored_note(owner, "Compression target ladder", System.Entity.CONCEPT))
        await written.keep(
            user,
            (finding(url="https://attacker.test/page", text=crafted),),
            Freshness.stable,
            frozenset({owner}),
        )
        async with user as session:
            note = (
                await session.exec(
                    select(Document).where(Document.origin == Document.Origin.authored)
                )
            ).one()
            cached = list(
                await session.exec(
                    select(Document).where(Document.origin == Document.Origin.web_cache)
                )
            )
        return note, cached

    note, cached = dbutil.run(body())

    # the note keeps its own text, its subject, and no borrowed expiry
    assert note.title == "Compression target ladder"
    assert note.subject_type == System.Entity.CONCEPT
    assert note.expires_at is None
    # the page landed as its own separate document, claiming no subject of its own
    assert len(cached) == 1
    assert cached[0].subject_type is None
    assert cached[0].id != note.id


def test_a_page_sharing_an_authored_source_url_stays_a_separate_document() -> None:
    """The same-URL case, which the namespaced locator keeps out of the note's unique slot."""
    owner = uuid5()
    user = User.private(owner)
    written, _ = cache()
    shared = "https://example.test/shared"

    async def body() -> list[Document]:
        async with user as session:
            note = authored_note(owner, "An authored note about a page")
            note.source_uri = shared
            session.add(note)
        await written.keep(user, (finding(url=shared),), Freshness.stable, frozenset({owner}))
        async with user as session:
            return list(await session.exec(select(Document).order_by(Document.origin)))

    stored = dbutil.run(body())

    assert [document.origin for document in stored] == [
        Document.Origin.authored,
        Document.Origin.web_cache,
    ]
    assert stored[0].source_uri == shared
    assert stored[1].source_uri == Document.cache_locator(shared)


class RefusingCache(WebCache):
    """A cache whose store rejects every page, which a crafted page really can provoke."""

    async def write(
        self, user: User, finding: WebFinding, expires_at: datetime, scopes: Scopes
    ) -> None:
        del user, finding, expires_at, scopes
        raise ValueError("no ontology kind named Widget")


def test_a_page_the_store_refuses_is_still_answered_with() -> None:
    """A crafted page must never destroy the memory half of the answer it rode in on."""
    owner = uuid5()
    user = User.private(owner)
    refusing = RefusingCache.build(settings, as_scanner(ScriptedScanner()))

    async def body() -> tuple[WebFinding, ...]:
        return await refusing.keep(user, (finding(),), Freshness.stable, frozenset({owner}))

    assert len(dbutil.run(body())) == 1


def test_a_refetched_cached_page_never_reopens_its_chunks_for_projection() -> None:
    """A metadata-only refresh of a quarantined page would otherwise leave chunks pending."""
    owner = uuid5()
    user = User.private(owner)
    written, _ = cache()
    url = "https://example.test/metadata"

    async def body() -> list[Chunk]:
        page = finding(url=url, text="the page body that never changes")
        await written.keep(user, (page,), Freshness.stable, frozenset({owner}))
        async with user as session:
            stored = (
                await session.exec(
                    select(Document).where(Document.source_uri == Document.cache_locator(url))
                )
            ).one()
            await session.exec(
                update(Chunk)
                .where(Chunk.document_id == stored.id)
                .values(processed_at=datetime.now(UTC))
            )
        # the same bytes with a new expiry take the metadata path rather than a re-chunk
        await written.keep(user, (page,), Freshness.volatile, frozenset({owner}))
        async with user as session:
            return list(await session.exec(select(Chunk).where(Chunk.document_id == stored.id)))

    chunks = dbutil.run(body())

    assert chunks and all(chunk.processed_at is not None for chunk in chunks)
