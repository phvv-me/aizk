import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from patos import FrozenModel
from sqlalchemy import ColumnElement, or_
from sqlmodel import delete, select

from ..config import settings
from ..provenance import CaptureContext
from ..serving.chunk import chunk_code, chunk_text, is_code, is_text
from ..serving.embed import embed, embed_images
from ..store import Chunk, Document, FactClaim, SessionItem
from ..store.engine import Session
from ..store.identity import User
from ..types import Scopes


def content_hash(text: str) -> str:
    """Digest the source text so re-ingesting identical content is a no-op."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def contextual_lexical(title: str, text: str, capture: CaptureContext | None = None) -> str | None:
    """The lexical-lane text for a chunk, enriched with document and speaker context."""
    preamble = title.strip() if settings.contextual_bm25 else ""
    searchable = capture.search_text(text) if capture is not None else text
    if not preamble and searchable == text:
        return None
    return "\n".join(part for part in (preamble, searchable) if part)


class TextSource(FrozenModel):
    """One text source ready for batched chunking, embedding, and storage."""

    text: str
    title: str | None = None
    kind: str = "note"
    source_uri: str | None = None
    created_by: uuid.UUID | None = None
    scopes: Scopes = frozenset()
    capture: CaptureContext | None = None
    processed: bool = False


class PreparedText(FrozenModel):
    """A nonempty text source after identity, chunk, and search text preparation."""

    source: TextSource
    title: str
    digest: str
    created_by: uuid.UUID
    scopes: Scopes
    spans: tuple[str, ...]
    searchable: tuple[str, ...]


class DocumentStore:
    """Store and refresh documents inside one caller-owned transaction."""

    __slots__ = ("session",)

    def __init__(self, opened: Session) -> None:
        self.session = opened

    async def find(self, plans: list[PreparedText]) -> list[Document | None]:
        """Find every standing document for a prepared batch in one query."""
        if not plans:
            return []
        conditions: list[ColumnElement[bool]] = [
            (
                Document.source_uri == plan.source.source_uri
                if plan.source.source_uri is not None
                else Document.content_hash == plan.digest
            )
            & (Document.scopes == sorted(plan.scopes))
            for plan in plans
        ]
        documents = list(await self.session.exec(select(Document).where(or_(*conditions))))
        by_source = {
            (document.source_uri, frozenset(document.scopes)): document
            for document in documents
            if document.source_uri is not None
        }
        by_hash = {
            (document.content_hash, frozenset(document.scopes)): document for document in documents
        }
        return [
            (
                by_source.get((plan.source.source_uri, plan.scopes))
                if plan.source.source_uri is not None
                else by_hash.get((plan.digest, plan.scopes))
            )
            for plan in plans
        ]

    async def store(
        self, dedupe: ColumnElement[bool], document: Document
    ) -> tuple[uuid.UUID, bool]:
        """Dedupe-check then store or refresh a document in its exact scope."""
        exact_scope = Document.scopes == document.scopes
        existing = (await self.session.exec(select(Document).where(dedupe, exact_scope))).first()
        if existing is not None:
            if existing.content_hash == document.content_hash:
                return existing.id, False
            return await self.refresh(existing, document), True
        self.session.add(document)
        await self.session.flush()
        return document.id, True

    async def refresh(self, stale: Document, document: Document) -> uuid.UUID:
        """Replace a changed document's chunks while retaining its stable identity."""
        replacements = list(document.chunks)
        document.chunks = []
        stale.title = document.title
        stale.kind = document.kind
        stale.content_hash = document.content_hash
        await FactClaim.retract_from_documents(self.session, [stale.id], "source_refreshed")
        await self.session.exec(
            delete(Chunk)
            .where(Chunk.document_id == stale.id)
            .execution_options(synchronize_session=False)
        )
        for chunk in replacements:
            chunk.document_id = stale.id
            chunk.created_by = stale.created_by
            chunk.scopes = list(stale.scopes)
            self.session.add(chunk)
        await self.session.flush()
        return stale.id


class TextIngestor:
    """Batch text ingestion so many messages share the embedder's efficient request batches."""

    __slots__ = ("user",)

    def __init__(self, user: User) -> None:
        self.user = user

    def prepare(self, source: TextSource) -> PreparedText | None:
        """Resolve one source and return its nonempty chunk plan, or null for blank text."""
        spans = chunk_code(source.text) if source.kind == "code" else chunk_text(source.text)
        if not spans:
            return None
        created_by = source.created_by or settings.system_user_id
        title = source.title or " ".join(source.text.split()[:8])
        searchable = tuple(
            source.capture.search_text(span) if source.capture is not None else span
            for span in spans
        )
        return PreparedText(
            source=source,
            title=title,
            digest=content_hash(source.text),
            created_by=created_by,
            scopes=frozenset(source.scopes or (created_by,)),
            spans=tuple(spans),
            searchable=searchable,
        )

    async def ingest_many(
        self, sources: Sequence[TextSource]
    ) -> list[tuple[uuid.UUID | None, bool]]:
        """Ingest sources in order after removing unchanged documents before embedding."""
        plans = [self.prepare(source) for source in sources]
        prepared = [plan for plan in plans if plan is not None]
        async with self.user as opened:
            existing = iter(await DocumentStore(opened).find(prepared))
        standing = [next(existing) if plan is not None else None for plan in plans]
        pending = [
            plan
            for plan, document in zip(plans, standing, strict=True)
            if plan is not None and (document is None or document.content_hash != plan.digest)
        ]
        searchable = [text for plan in pending for text in plan.searchable]
        vectors = await embed(searchable, mode="document") if searchable else []
        offset = 0
        results: list[tuple[uuid.UUID | None, bool]] = []
        async with self.user as opened:
            store = DocumentStore(opened)
            for plan, document in zip(plans, standing, strict=True):
                if plan is None:
                    results.append((None, False))
                    continue
                if document is not None and document.content_hash == plan.digest:
                    results.append((document.id, False))
                    continue
                embeddings = vectors[offset : offset + len(plan.spans)]
                offset += len(plan.spans)
                document = self.document(plan, embeddings)
                dedupe = (
                    Document.source_uri == plan.source.source_uri
                    if plan.source.source_uri is not None
                    else Document.content_hash == plan.digest
                )
                document_id, created = await store.store(dedupe, document)
                logger.info("resolved document {} kind={}", document_id, plan.source.kind)
                results.append((document_id, created))
        return results

    async def ingest(self, source: TextSource) -> tuple[uuid.UUID | None, bool]:
        """Ingest one source through the same batching path used for a corpus."""
        return (await self.ingest_many([source]))[0]

    @staticmethod
    def document(plan: PreparedText, embeddings: list[list[float]]) -> Document:
        """Build the mapped document and chunk rows for one prepared source."""
        capture = plan.source.capture
        return Document(
            kind=plan.source.kind,
            title=plan.title,
            source_uri=plan.source.source_uri,
            content_hash=plan.digest,
            created_by=plan.created_by,
            scopes=list(plan.scopes),
            chunks=[
                Chunk(
                    ord=order,
                    text=span,
                    lexical=contextual_lexical(plan.title, span, capture),
                    provenance=capture.record() if capture is not None else {},
                    embedding=embedding,
                    processed_at=datetime.now(UTC) if plan.source.processed else None,
                    created_by=plan.created_by,
                    scopes=list(plan.scopes),
                )
                for order, (span, embedding) in enumerate(zip(plan.spans, embeddings, strict=True))
            ],
        )


async def store_document(
    user: User, dedupe: ColumnElement[bool], document: Document
) -> tuple[uuid.UUID, bool]:
    """Dedupe-check then store or refresh a document in one exact scope transaction."""
    async with user as opened:
        return await DocumentStore(opened).store(dedupe, document)


async def ingest_image(
    user: User,
    path: Path,
    title: str | None = None,
    caption: str | None = None,
    created_by: uuid.UUID | None = None,
    scopes: Scopes = frozenset(),
) -> uuid.UUID:
    """Store an image as a document whose one chunk embeds into the shared multimodal space."""
    created_by = created_by or settings.system_user_id
    key = frozenset(scopes or (created_by,))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    [embedding] = await embed_images([str(path)])
    document = Document(
        kind="image",
        title=title or path.stem,
        source_uri=path.resolve().as_uri(),
        content_hash=digest,
        created_by=created_by,
        scopes=sorted(key),
        chunks=[
            Chunk(
                ord=0,
                text=caption or path.name,
                embedding=embedding,
                created_by=created_by,
                scopes=sorted(key),
            )
        ],
    )
    document_id, created = await store_document(user, Document.content_hash == digest, document)
    if created:
        logger.info("ingested image {} from {}", document_id, path)
    return document_id


def text_files(path: Path) -> list[Path]:
    """The text files under path, itself when it is one file, else every text file below it."""
    candidates = [path] if path.is_file() else sorted(path.rglob("*"))
    return [file for file in candidates if is_text(file)]


async def ingest_path(
    user: User,
    path: Path,
    created_by: uuid.UUID | None = None,
    scopes: Scopes = frozenset(),
) -> int:
    """Ingest every supported file under path and return the documents stored."""
    created_by = created_by or settings.system_user_id
    key = frozenset(scopes or (created_by,))
    logger.info("ingest start path={}", path)
    sources = [
        TextSource(
            text=file.read_text(encoding="utf-8", errors="replace"),
            title=file.stem,
            kind="code" if is_code(file) else "note",
            source_uri=file.resolve().as_uri(),
            created_by=created_by,
            scopes=key,
        )
        for file in text_files(path)
    ]
    ingested = sum(created for _, created in await TextIngestor(user).ingest_many(sources))
    logger.info("ingest done documents={}", ingested)
    return ingested


async def ingest_text(
    user: User,
    text: str,
    title: str | None = None,
    kind: str = "note",
    source_uri: str | None = None,
    created_by: uuid.UUID | None = None,
    scopes: Scopes = frozenset(),
    capture: CaptureContext | None = None,
) -> uuid.UUID | None:
    """Store a raw text blob as a document with embedded chunks and return its id."""
    document_id, _ = await TextIngestor(user).ingest(
        TextSource(
            text=text,
            title=title,
            kind=kind,
            source_uri=source_uri,
            created_by=created_by,
            scopes=scopes,
            capture=capture,
        )
    )
    return document_id


async def ingest_texts(user: User, sources: Sequence[TextSource]) -> list[uuid.UUID | None]:
    """Batch a corpus of text sources through one chunk preparation and embedding pipeline."""
    return [document_id for document_id, _ in await TextIngestor(user).ingest_many(sources)]


async def record_reference(
    user: User,
    uri: str,
    title: str | None = None,
    created_by: uuid.UUID | None = None,
    scopes: Scopes = frozenset(),
) -> uuid.UUID:
    """Store an embedded reference pointer as an already-processed searchable document."""
    created_by = created_by or settings.system_user_id
    key = frozenset(scopes or (created_by,))
    document_id, _ = await TextIngestor(user).ingest(
        TextSource(
            text=uri,
            title=title or uri,
            kind="reference",
            source_uri=uri,
            created_by=created_by,
            scopes=key,
            processed=True,
        )
    )
    assert document_id is not None
    return document_id


async def remember_session(
    user: User,
    text: str,
    kind: str = "note",
    created_by: uuid.UUID | None = None,
    scopes: Scopes = frozenset(),
    capture: CaptureContext | None = None,
) -> uuid.UUID:
    """Store a remembered blob as one working-memory item and return its id, the cheap front
    write."""
    created_by = created_by or settings.system_user_id
    key = frozenset(scopes or (created_by,))
    searchable = capture.search_text(text) if capture is not None else text
    [embedding] = await embed([searchable], mode="document")
    async with user as session:
        item = SessionItem(
            kind=kind,
            text=text,
            provenance=capture.record() if capture is not None else {},
            embedding=embedding,
            created_by=created_by,
            scopes=sorted(key),
        )
        session.add(item)
        await session.flush()
        logger.info("remembered session item {} kind={}", item.id, kind)
        return item.id
