import hashlib
import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from ..config import settings
from ..serving.chunk import ChonkieChunker, CodeChunker, is_code, is_text
from ..serving.embed import Embedder
from ..store import Chunk, Document, SessionItem, acting_as

# re-exported so aizk.extract.ingest keeps naming this directory-walk filter, even though the
# detection it runs lives in aizk.serving.chunk.
__all__ = ["is_text"]


def content_hash(text: str) -> str:
    """Digest the source text so re-ingesting identical content is a no-op.

    text: full document text.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def contextual_lexical(title: str, text: str) -> str | None:
    """The lexical-lane text for a chunk, a title-prefixed preamble when contextual bm25 is on.

    Prepending the title lets a chunk match on its document's context in the lexical lane, feeding
    only the separate `lexical` column so the dense embedding and displayed chunk text stay the raw
    span. Returns null when the flag is off or the title is blank, so the lexical lane falls back
    to the raw span through the generated column's coalesce.

    title: the document title the preamble situates the chunk under.
    text: the raw chunk span the preamble is prepended to.
    """
    preamble = title.strip()
    if not settings.contextual_bm25 or not preamble:
        return None
    return f"{preamble}\n{text}"


async def ingest_image(
    path: Path,
    title: str | None = None,
    caption: str | None = None,
    owner_id: uuid.UUID | None = None,
    scope: uuid.UUID | None = None,
) -> uuid.UUID:
    """Store an image as a document whose one chunk embeds into the shared multimodal space.

    The image is embedded through the embedder's image lane into the same halfvec space the text
    chunks live in, so a later text query recalls it by plain cosine without a separate index. The
    write dedupes on the image bytes' content hash so re-ingesting the same picture is idempotent.

    path: image file to ingest.
    title: human-readable label, defaulted from the file stem when null.
    caption: text stored on the chunk, defaulted to the file name when null.
    owner_id: principal that owns the stored rows, the system principal when null.
    scope: group the stored rows are shared with, null when private to the owner.
    """
    owner_id = owner_id or settings.system_principal_id
    embedder = Embedder()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    [embedding] = await embedder.embed_images([str(path)])
    async with acting_as(owner_id) as session:
        existing = await session.scalar(select(Document.id).where(Document.content_hash == digest))
        if existing is not None:
            return existing
        document = Document(
            kind="image",
            title=title or path.stem,
            source_uri=path.resolve().as_uri(),
            content_hash=digest,
            owner_id=owner_id,
            scope=scope,
            chunks=[
                Chunk(
                    ord=0,
                    text=caption or path.name,
                    embedding=embedding,
                    owner_id=owner_id,
                    scope=scope,
                )
            ],
        )
        session.add(document)
        await session.flush()
        logger.info("ingested image {} from {}", document.id, path)
        return document.id


async def ingest_path(
    path: Path,
    owner_id: uuid.UUID | None = None,
    scope: uuid.UUID | None = None,
) -> int:
    """Ingest every supported file under path and return the documents stored.

    Skips a file whose content hash already matches a stored Document. A source file (kind=code)
    is split by the code chunker and a note (kind=note) by the prose one, then each document with
    its embedded chunks writes in one owner-scoped transaction.

    path: file or directory to ingest.
    owner_id: principal that owns the stored rows, the system principal when null.
    scope: group the stored rows are shared with, null when private to the owner.
    """
    owner_id = owner_id or settings.system_principal_id
    logger.info("ingest start path={}", path)
    embedder = Embedder()
    candidates = [path] if path.is_file() else sorted(path.rglob("*"))
    files = [file for file in candidates if is_text(file)]
    ingested = 0
    for file in files:
        content = file.read_text(encoding="utf-8")
        digest = content_hash(content)
        code = is_code(file)
        spans = (CodeChunker() if code else ChonkieChunker()).chunk(content)
        if not spans:
            continue
        kind = "code" if code else "note"
        async with acting_as(owner_id) as session:
            existing = await session.scalar(
                select(Document.id).where(Document.content_hash == digest)
            )
            if existing is not None:
                continue
            embeddings = await embedder.embed(spans, mode="document")
            session.add(
                Document(
                    kind=kind,
                    title=file.stem,
                    source_uri=file.resolve().as_uri(),
                    content_hash=digest,
                    owner_id=owner_id,
                    scope=scope,
                    chunks=[
                        Chunk(
                            ord=order,
                            text=span,
                            lexical=contextual_lexical(file.stem, span),
                            embedding=embedding,
                            owner_id=owner_id,
                            scope=scope,
                        )
                        for order, (span, embedding) in enumerate(
                            zip(spans, embeddings, strict=True)
                        )
                    ],
                )
            )
        ingested += 1
    logger.info("ingest done documents={}", ingested)
    return ingested


async def ingest_text(
    text: str,
    title: str | None = None,
    kind: str = "note",
    owner_id: uuid.UUID | None = None,
    scope: uuid.UUID | None = None,
) -> uuid.UUID:
    """Store a raw text blob as a document with embedded chunks and return its id.

    Chunks the text with the prose chunker, embeds the spans, and writes the document and its
    chunks in one owner-scoped transaction, returning the existing id when the same content was
    stored before so a remember of identical text is idempotent. Graph extraction is enqueued by
    the caller, since this only lands the rows.

    text: the content to remember.
    title: human-readable label, defaulted from the leading words when null.
    kind: coarse type tag stamped on the document, such as note or code.
    owner_id: principal that owns the stored rows, the system principal when null.
    scope: group the stored rows are shared with, null when private to the owner.
    """
    owner_id = owner_id or settings.system_principal_id
    digest = content_hash(text)
    effective_title = title or " ".join(text.split()[:8])
    spans = ChonkieChunker().chunk(text)
    embeddings = await Embedder().embed(spans, mode="document")
    async with acting_as(owner_id) as session:
        existing = await session.scalar(select(Document.id).where(Document.content_hash == digest))
        if existing is not None:
            return existing
        document = Document(
            kind=kind,
            title=effective_title,
            content_hash=digest,
            owner_id=owner_id,
            scope=scope,
            chunks=[
                Chunk(
                    ord=order,
                    text=span,
                    lexical=contextual_lexical(effective_title, span),
                    embedding=embedding,
                    owner_id=owner_id,
                    scope=scope,
                )
                for order, (span, embedding) in enumerate(zip(spans, embeddings, strict=True))
            ],
        )
        session.add(document)
        await session.flush()
        logger.info("remembered document {} kind={}", document.id, kind)
        return document.id


async def record_reference(
    uri: str,
    title: str | None = None,
    owner_id: uuid.UUID | None = None,
    scope: uuid.UUID | None = None,
) -> uuid.UUID:
    """Record a pointer to an external paper, url, or file as a reference document.

    Stores a chunkless Document stamped kind=reference whose source_uri is the locator, so the
    reference is recallable by title and deduped on its uri, returning the existing id when that
    uri was recorded before. Fetching and chunking the target is a later enrichment pass.

    uri: locator of the paper, url, or file to reference.
    title: human-readable label, defaulted to the uri when null.
    owner_id: principal that owns the stored row, the system principal when null.
    scope: group the row is shared with, null when private to the owner.
    """
    owner_id = owner_id or settings.system_principal_id
    async with acting_as(owner_id) as session:
        existing = await session.scalar(select(Document.id).where(Document.source_uri == uri))
        if existing is not None:
            return existing
        document = Document(
            kind="reference",
            title=title or uri,
            source_uri=uri,
            content_hash=content_hash(uri),
            owner_id=owner_id,
            scope=scope,
        )
        session.add(document)
        await session.flush()
        logger.info("recorded reference {} uri={}", document.id, uri)
        return document.id


async def remember_session(
    text: str,
    kind: str = "note",
    owner_id: uuid.UUID | None = None,
    scope: uuid.UUID | None = None,
) -> uuid.UUID:
    """Store a remembered blob as one working-memory item and return its id, the cheap front write.

    A remember lands here first rather than paying the chunk, embed, and extract pipeline up
    front, so a capture is a single embedded row the recall lane can already rank. The promotion
    pass later feeds the aged or overflow items into the long-term graph. The write runs as
    owner_id so the row level security write policy admits it.

    text: the content to remember.
    kind: coarse type tag carried through to the promoted document.
    owner_id: principal that owns the stored item, the system principal when null.
    scope: group the item is shared with, null when private to the owner.
    """
    owner_id = owner_id or settings.system_principal_id
    [embedding] = await Embedder().embed([text], mode="document")
    async with acting_as(owner_id) as session:
        item = SessionItem(
            kind=kind, text=text, embedding=embedding, owner_id=owner_id, scope=scope
        )
        session.add(item)
        await session.flush()
        logger.info("remembered session item {} kind={}", item.id, kind)
        return item.id
