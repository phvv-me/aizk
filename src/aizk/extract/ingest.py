import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from loguru import logger
from patos import FrozenModel, sql
from pydantic import UUID5, UUID7, UUID8
from sqlalchemy import Integer, LargeBinary, column, literal, or_
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import delete, select, update

from ..config import settings
from ..ontology import Ontology
from ..provenance import CaptureContext
from ..serving.chunk import chunk_text, is_text
from ..serving.embed import embed, embed_images
from ..store import Chunk, Document, Fact, SessionItem
from ..store.engine import Session
from ..store.identity import User
from ..types import Scopes
from .declaration import SourceDeclaration


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
    subject_type: str | None = None
    source_uri: str | None = None
    created_by: UUID5 | None = None
    scopes: Scopes = frozenset()
    capture: CaptureContext | None = None


class PreparedText(FrozenModel):
    """A nonempty text source after identity, chunk, and search text preparation."""

    source: TextSource
    title: str
    subject_type: str | None
    digest: UUID8
    created_by: UUID5
    scopes: Scopes
    spans: tuple[str, ...]
    searchable: tuple[str, ...]

    def content_matches(self, document: Document | None) -> bool:
        """Whether the stored chunks and structural identity match this source."""
        return document is not None and (
            document.content_hash,
            document.title,
            document.subject_type,
        ) == (
            self.digest,
            self.title,
            self.subject_type,
        )

    def matches(self, document: Document | None) -> bool:
        """Whether stored content and management metadata match this source."""
        return (
            self.content_matches(document)
            and document is not None
            and (
                document.source_uri,
                document.observed_at,
                document.expires_at,
            )
            == (
                self.source.source_uri,
                self.source.capture.observed_at if self.source.capture is not None else None,
                self.source.capture.expires_at if self.source.capture is not None else None,
            )
        )


class DocumentStore:
    """Store and refresh documents inside one caller-owned transaction."""

    __slots__ = ("session",)

    def __init__(self, opened: Session) -> None:
        self.session = opened

    async def hash_texts(self, texts: Sequence[str]) -> list[UUID8]:
        """Hash text inputs in order through PostgreSQL `pgcrypto`."""
        if not texts:
            return []
        inputs = sql.relation(
            "document_hash_inputs",
            (
                column("ordinal", Integer),
                column("content", LargeBinary),
            ),
            [(ordinal, text.encode("utf-8")) for ordinal, text in enumerate(texts)],
        )
        digest = sql.uuid8(cast(ColumnElement[bytes], inputs.c.content))
        return list(await self.session.exec(select(digest).order_by(inputs.c.ordinal)))

    async def hash_bytes(self, raw: bytes) -> UUID8:
        """Hash binary content through PostgreSQL `pgcrypto`."""
        digest = sql.uuid8(literal(raw, type_=LargeBinary))
        return (await self.session.exec(select(digest))).one()

    async def find(self, plans: list[PreparedText]) -> list[Document | None]:
        """Find every standing document for a prepared batch in one query."""
        if not plans:
            return []
        matches = [
            Document.identifies(
                subject_type=plan.subject_type,
                title=plan.title,
                source_uri=plan.source.source_uri,
                content_hash=plan.digest,
            )
            & (Document.scopes == sorted(plan.scopes))
            for plan in plans
        ]
        documents = list(await self.session.exec(select(Document).where(or_(*matches))))
        indexed = {
            (
                Document.identity_key(
                    subject_type=document.subject_type,
                    title=document.title,
                    source_uri=document.source_uri,
                    content_hash=document.content_hash,
                ),
                frozenset(document.scopes),
            ): document
            for document in documents
        }
        return [
            indexed.get(
                (
                    Document.identity_key(
                        subject_type=plan.subject_type,
                        title=plan.title,
                        source_uri=plan.source.source_uri,
                        content_hash=plan.digest,
                    ),
                    plan.scopes,
                )
            )
            for plan in plans
        ]

    async def store(self, dedupe: ColumnElement[bool], document: Document) -> tuple[UUID7, bool]:
        """Dedupe-check then store or refresh a document in its exact scope."""
        exact_scope = Document.scopes == document.scopes
        existing = (await self.session.exec(select(Document).where(dedupe, exact_scope))).first()
        if existing is not None:
            if (
                existing.content_hash,
                existing.title,
                existing.subject_type,
            ) == (
                document.content_hash,
                document.title,
                document.subject_type,
            ):
                return existing.id, False
            return await self.refresh(existing, document), True
        self.session.add(document)
        await self.session.flush()
        return document.id, True

    async def refresh(self, stale: Document, document: Document) -> UUID7:
        """Replace a changed document's chunks while retaining its stable identity."""
        replacements = list(document.chunks)
        document.chunks = []
        stale.title = document.title
        stale.subject_type = document.subject_type
        stale.source_uri = document.source_uri
        stale.observed_at = document.observed_at
        stale.expires_at = document.expires_at
        stale.content_hash = document.content_hash
        await Fact.Claim.retract_from_documents(self.session, [stale.id], "source_refreshed")
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

    async def update_metadata(self, document_id: UUID7, source: TextSource) -> UUID7:
        """Update validity and requeue graph projection without re-embedding chunks."""
        capture = source.capture
        statement = (
            update(Document)
            .where(Document.id == document_id)
            .values(
                source_uri=source.source_uri,
                observed_at=capture.observed_at if capture is not None else None,
                expires_at=capture.expires_at if capture is not None else None,
            )
            .returning(Document.id)
        )
        resolved = (await self.session.exec(statement)).scalar_one()
        await Fact.Claim.retract_from_documents(
            self.session, [document_id], "source_metadata_changed"
        )
        await self.session.exec(
            update(Chunk)
            .where(Chunk.document_id == document_id)
            .values(
                processed_at=None,
                provenance=capture.record() if capture is not None else {},
            )
            .execution_options(synchronize_session=False)
        )
        return resolved


class TextIngestor:
    """Batch text ingestion so many messages share the embedder's efficient request batches."""

    __slots__ = ("user",)

    def __init__(self, user: User) -> None:
        self.user = user

    def prepare(self, source: TextSource, digest: UUID8) -> PreparedText | None:
        """Resolve one source and return its nonempty chunk plan, or null for blank text."""
        spans = chunk_text(source.text)
        if not spans:
            return None
        created_by = source.created_by or settings.system_user_id
        declaration = SourceDeclaration.from_text(source.text, source.title)
        subject_type = source.subject_type or declaration.subject_type
        if subject_type is not None:
            subject_type = Ontology.current().entity_kind(subject_type)
        title = declaration.title or " ".join(source.text.split()[:8])
        searchable = tuple(
            source.capture.search_text(span) if source.capture is not None else span
            for span in spans
        )
        return PreparedText(
            source=source,
            title=title,
            subject_type=subject_type,
            digest=digest,
            created_by=created_by,
            scopes=frozenset(source.scopes or (created_by,)),
            spans=tuple(spans),
            searchable=searchable,
        )

    async def ingest_many(self, sources: Sequence[TextSource]) -> list[tuple[UUID7 | None, bool]]:
        """Ingest sources in order after removing unchanged documents before embedding."""
        plans, standing = await self._plans(sources)
        vectors = await self._vectors(plans, standing)
        return await self._store(plans, standing, vectors)

    async def _plans(
        self, sources: Sequence[TextSource]
    ) -> tuple[list[PreparedText | None], list[Document | None]]:
        """Hash and match sources before any embedding work begins."""
        async with self.user as opened:
            store = DocumentStore(opened)
            digests = await store.hash_texts([source.text for source in sources])
            plans = [
                self.prepare(source, digest)
                for source, digest in zip(sources, digests, strict=True)
            ]
            prepared = [plan for plan in plans if plan is not None]
            existing = iter(await store.find(prepared))
        standing = [next(existing) if plan is not None else None for plan in plans]
        return plans, standing

    @staticmethod
    async def _vectors(
        plans: list[PreparedText | None], standing: list[Document | None]
    ) -> list[list[float]]:
        """Embed only changed plans in their final storage order."""
        pending = [
            plan
            for plan, document in zip(plans, standing, strict=True)
            if plan is not None and not plan.content_matches(document)
        ]
        searchable = [text for plan in pending for text in plan.searchable]
        return await embed(searchable, mode="document") if searchable else []

    async def _store(
        self,
        plans: list[PreparedText | None],
        standing: list[Document | None],
        vectors: list[list[float]],
    ) -> list[tuple[UUID7 | None, bool]]:
        """Write changed plans while retaining the input order in the result."""
        offset = 0
        results: list[tuple[UUID7 | None, bool]] = []
        async with self.user as opened:
            store = DocumentStore(opened)
            for plan, document in zip(plans, standing, strict=True):
                if plan is None:
                    results.append((None, False))
                    continue
                if plan.matches(document):
                    assert document is not None
                    results.append((document.id, False))
                    continue
                if plan.content_matches(document):
                    assert document is not None
                    document_id = await store.update_metadata(document.id, plan.source)
                    results.append((document_id, True))
                    continue
                embeddings = vectors[offset : offset + len(plan.spans)]
                offset += len(plan.spans)
                document = self.document(plan, embeddings)
                dedupe = Document.identifies(
                    subject_type=plan.subject_type,
                    title=plan.title,
                    source_uri=plan.source.source_uri,
                    content_hash=plan.digest,
                )
                document_id, created = await store.store(dedupe, document)
                logger.info("resolved document {}", document_id)
                results.append((document_id, created))
        return results

    async def ingest(self, source: TextSource) -> tuple[UUID7 | None, bool]:
        """Ingest one source through the same batching path used for a corpus."""
        return (await self.ingest_many([source]))[0]

    @staticmethod
    def document(plan: PreparedText, embeddings: list[list[float]]) -> Document:
        """Build the mapped document and chunk rows for one prepared source."""
        capture = plan.source.capture
        document_id = uuid.uuid7()
        return Document(
            id=document_id,
            title=plan.title,
            subject_type=plan.subject_type,
            source_uri=plan.source.source_uri,
            observed_at=capture.observed_at if capture is not None else None,
            expires_at=capture.expires_at if capture is not None else None,
            content_hash=plan.digest,
            created_by=plan.created_by,
            scopes=list(plan.scopes),
            chunks=[
                Chunk(
                    document_id=document_id,
                    ord=order,
                    text=span,
                    lexical=contextual_lexical(plan.title, span, capture),
                    provenance=capture.record() if capture is not None else {},
                    embedding=embedding,
                    created_by=plan.created_by,
                    scopes=list(plan.scopes),
                )
                for order, (span, embedding) in enumerate(zip(plan.spans, embeddings, strict=True))
            ],
        )


async def store_document(
    user: User, dedupe: ColumnElement[bool], document: Document
) -> tuple[UUID7, bool]:
    """Dedupe-check then store or refresh a document in one exact scope transaction."""
    async with user as opened:
        return await DocumentStore(opened).store(dedupe, document)


async def ingest_image(
    user: User,
    path: Path,
    title: str | None = None,
    caption: str | None = None,
    created_by: UUID5 | None = None,
    scopes: Scopes = frozenset(),
) -> UUID7:
    """Store an image as a document whose one chunk embeds into the shared multimodal space."""
    created_by = created_by or settings.system_user_id
    key = frozenset(scopes or (created_by,))
    async with user as opened:
        digest = await DocumentStore(opened).hash_bytes(path.read_bytes())
    [embedding] = await embed_images([str(path)])
    document_id = uuid.uuid7()
    document = Document(
        id=document_id,
        title=title or path.stem,
        source_uri=path.resolve().as_uri(),
        content_hash=digest,
        created_by=created_by,
        scopes=sorted(key),
        chunks=[
            Chunk(
                document_id=document_id,
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
    created_by: UUID5 | None = None,
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
    source_uri: str | None = None,
    created_by: UUID5 | None = None,
    scopes: Scopes = frozenset(),
    capture: CaptureContext | None = None,
) -> UUID7 | None:
    """Store a raw text blob as a document with embedded chunks and return its id."""
    document_id, _ = await TextIngestor(user).ingest(
        TextSource(
            text=text,
            title=title,
            source_uri=source_uri,
            created_by=created_by,
            scopes=scopes,
            capture=capture,
        )
    )
    return document_id


async def ingest_texts(user: User, sources: Sequence[TextSource]) -> list[UUID7 | None]:
    """Batch a corpus of text sources through one chunk preparation and embedding pipeline."""
    return [document_id for document_id, _ in await TextIngestor(user).ingest_many(sources)]


async def remember_session(
    user: User,
    text: str,
    kind: str = "note",
    created_by: UUID5 | None = None,
    scopes: Scopes = frozenset(),
    capture: CaptureContext | None = None,
) -> UUID7:
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
