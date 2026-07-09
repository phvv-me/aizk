import hashlib
import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import ColumnElement, select

from ..config import settings
from ..serving.chunk import ChonkieChunker, CodeChunker, is_code, is_text
from ..serving.embed import Embedder
from ..store import Chunk, Document, SessionItem, acting_as
from ..store.context import session

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


async def store_document(
    owner_id: uuid.UUID, dedupe: ColumnElement[bool], document: Document
) -> tuple[uuid.UUID, bool]:
    """Dedupe-check then store or refresh a document under one owner-scoped transaction.

    The one seam every ingest path shares: look up an existing row by its own dedupe column, and
    only write when something changed, so a re-ingest of identical content is idempotent no
    matter which caller built the row. A source whose content CHANGED (same ``source_uri``,
    different hash, the edited note case) keeps its standing document row and swaps the content
    under it through :func:`refresh_document`, since ``source_uri`` is the document's stable
    identity and a second insert would violate its unique constraint.

    owner_id: user the transaction acts as.
    dedupe: the `Document` column-equality clause idempotency is checked against before insert.
    document: the fully assembled row to store when nothing already matches.

    Returns the row's id and whether it was written, the latter False only when dedupe matched.
    """
    async with acting_as(owner_id):
        existing = await session().scalar(select(Document.id).where(dedupe))
        if existing is not None:
            return existing, False
        if document.source_uri is not None:
            stale = await session().scalar(
                select(Document).where(Document.source_uri == document.source_uri)
            )
            if stale is not None:
                return await refresh_document(stale, document), True
        session().add(document)
        await session().flush()
        return document.id, True


async def refresh_document(stale: Document, document: Document) -> uuid.UUID:
    """Swap a changed source's content under its standing document row, replacing every chunk.

    The row is the source's stable identity (``source_uri`` is unique), so an edited file
    updates it in place: title, kind and content hash move to the fresh values, the old chunks
    are deleted and the fresh spans inserted with ``processed_at`` null so the next graph build
    re-extracts them. Claims minted from the old spans stay live with their provenance nulled
    (``source_chunk_id`` is SET NULL on chunk delete), and the re-extraction's consolidation
    supersedes whatever the new content contradicts, the bi-temporal record intact. Ownership
    and scopes stay the standing row's, a refresh changes content, never sharing.

    stale: the standing document row whose content changed.
    document: the freshly assembled row carrying the new content and chunks.
    """
    replacements = list(document.chunks)
    document.chunks = []
    stale.title = document.title
    stale.kind = document.kind
    stale.content_hash = document.content_hash
    for old in await session().scalars(select(Chunk).where(Chunk.document_id == stale.id)):
        await session().delete(old)
    for chunk in replacements:
        chunk.document_id = stale.id
        chunk.owner_id = stale.owner_id
        chunk.scopes = list(stale.scopes)
        session().add(chunk)
    await session().flush()
    return stale.id


async def ingest_image(
    path: Path,
    title: str | None = None,
    caption: str | None = None,
    owner_id: uuid.UUID | None = None,
    scopes: tuple[uuid.UUID, ...] = (),
) -> uuid.UUID:
    """Store an image as a document whose one chunk embeds into the shared multimodal space.

    The image is embedded through the embedder's image lane into the same halfvec space the text
    chunks live in, so a later text query recalls it by plain cosine without a separate index. The
    write dedupes on the image bytes' content hash so re-ingesting the same picture is idempotent.

    path: image file to ingest.
    title: human-readable label, defaulted from the file stem when null.
    caption: text stored on the chunk, defaulted to the file name when null.
    owner_id: user that owns the stored rows, the system user when null.
    scopes: group set the stored rows are shared with, private to the owner when empty.
    """
    owner_id = owner_id or settings.system_user_id
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    [embedding] = await Embedder().embed_images([str(path)])
    document = Document(
        kind="image",
        title=title or path.stem,
        source_uri=path.resolve().as_uri(),
        content_hash=digest,
        owner_id=owner_id,
        scopes=list(scopes),
        chunks=[
            Chunk(
                ord=0,
                text=caption or path.name,
                embedding=embedding,
                owner_id=owner_id,
                scopes=list(scopes),
            )
        ],
    )
    document_id, created = await store_document(
        owner_id, Document.content_hash == digest, document
    )
    if created:
        logger.info("ingested image {} from {}", document_id, path)
    return document_id


def text_files(path: Path) -> list[Path]:
    """The text files under path, itself when it is one file, else every text file below it.

    path: file or directory to enumerate.
    """
    candidates = [path] if path.is_file() else sorted(path.rglob("*"))
    return [file for file in candidates if is_text(file)]


async def ingest_file(
    file: Path, owner_id: uuid.UUID, scopes: tuple[uuid.UUID, ...], embedder: Embedder
) -> bool:
    """Chunk, embed, and store one file as a document, returning whether it was written.

    Skips a file whose content already matches a stored document, or one whose chunker returns no
    spans at all, such as an empty file. A file stored before under the same ``source_uri`` whose
    content CHANGED refreshes its standing document in place, counting as written. A byte the
    `is_text` filter admitted but utf-8 cannot decode is replaced rather than raised, so one
    malformed file never aborts the surrounding directory walk.

    file: source file to ingest.
    owner_id: user that owns the stored rows.
    scopes: group set the stored rows are shared with.
    embedder: the shared embedder every file in the walk reuses.
    """
    content = file.read_text(encoding="utf-8", errors="replace")
    digest = content_hash(content)
    code = is_code(file)
    spans = (CodeChunker() if code else ChonkieChunker()).chunk(content)
    if not spans:
        return False
    embeddings = await embedder.embed(spans, mode="document")
    document = Document(
        kind="code" if code else "note",
        title=file.stem,
        source_uri=file.resolve().as_uri(),
        content_hash=digest,
        owner_id=owner_id,
        scopes=list(scopes),
        chunks=[
            Chunk(
                ord=order,
                text=span,
                lexical=contextual_lexical(file.stem, span),
                embedding=embedding,
                owner_id=owner_id,
                scopes=list(scopes),
            )
            for order, (span, embedding) in enumerate(zip(spans, embeddings, strict=True))
        ],
    )
    _, created = await store_document(owner_id, Document.content_hash == digest, document)
    return created


async def ingest_path(
    path: Path,
    owner_id: uuid.UUID | None = None,
    scopes: tuple[uuid.UUID, ...] = (),
) -> int:
    """Ingest every supported file under path and return the documents stored.

    Skips a file whose content hash already matches a stored Document. A source file (kind=code)
    is split by the code chunker and a note (kind=note) by the prose one, then each document with
    its embedded chunks writes in one owner-scoped transaction.

    path: file or directory to ingest.
    owner_id: user that owns the stored rows, the system user when null.
    scopes: group set the stored rows are shared with, private to the owner when empty.
    """
    owner_id = owner_id or settings.system_user_id
    logger.info("ingest start path={}", path)
    embedder = Embedder()
    written = [await ingest_file(file, owner_id, scopes, embedder) for file in text_files(path)]
    ingested = sum(written)
    logger.info("ingest done documents={}", ingested)
    return ingested


async def ingest_text(
    text: str,
    title: str | None = None,
    kind: str = "note",
    owner_id: uuid.UUID | None = None,
    scopes: tuple[uuid.UUID, ...] = (),
) -> uuid.UUID | None:
    """Store a raw text blob as a document with embedded chunks and return its id.

    Chunks the text with the prose chunker, embeds the spans, and writes the document and its
    chunks in one owner-scoped transaction, returning the existing id when the same content was
    stored before so a remember of identical text is idempotent. An empty or whitespace blob the
    chunker yields no spans for returns null without writing a chunkless dead document, mirroring
    `ingest_file`'s own empty-file guard. Graph extraction is enqueued by the caller, since this
    only lands the rows.

    text: the content to remember.
    title: human-readable label, defaulted from the leading words when null.
    kind: coarse type tag stamped on the document, such as note or code.
    owner_id: user that owns the stored rows, the system user when null.
    scopes: group set the stored rows are shared with, private to the owner when empty.
    """
    owner_id = owner_id or settings.system_user_id
    digest = content_hash(text)
    effective_title = title or " ".join(text.split()[:8])
    spans = ChonkieChunker().chunk(text)
    if not spans:
        return None
    embeddings = await Embedder().embed(spans, mode="document")
    document = Document(
        kind=kind,
        title=effective_title,
        content_hash=digest,
        owner_id=owner_id,
        scopes=list(scopes),
        chunks=[
            Chunk(
                ord=order,
                text=span,
                lexical=contextual_lexical(effective_title, span),
                embedding=embedding,
                owner_id=owner_id,
                scopes=list(scopes),
            )
            for order, (span, embedding) in enumerate(zip(spans, embeddings, strict=True))
        ],
    )
    document_id, created = await store_document(
        owner_id, Document.content_hash == digest, document
    )
    if created:
        logger.info("remembered document {} kind={}", document_id, kind)
    return document_id


async def record_reference(
    uri: str,
    title: str | None = None,
    owner_id: uuid.UUID | None = None,
    scopes: tuple[uuid.UUID, ...] = (),
) -> uuid.UUID:
    """Record a pointer to an external paper, url, or file as a reference document.

    Stores a chunkless Document stamped kind=reference whose source_uri is the locator, so the
    reference is recallable by title and deduped on its uri, returning the existing id when that
    uri was recorded before. Fetching and chunking the target is a later enrichment pass.

    uri: locator of the paper, url, or file to reference.
    title: human-readable label, defaulted to the uri when null.
    owner_id: user that owns the stored row, the system user when null.
    scopes: group set the row is shared with, private to the owner when empty.
    """
    owner_id = owner_id or settings.system_user_id
    document = Document(
        kind="reference",
        title=title or uri,
        source_uri=uri,
        content_hash=content_hash(uri),
        owner_id=owner_id,
        scopes=list(scopes),
    )
    document_id, created = await store_document(owner_id, Document.source_uri == uri, document)
    if created:
        logger.info("recorded reference {} uri={}", document_id, uri)
    return document_id


async def remember_session(
    text: str,
    kind: str = "note",
    owner_id: uuid.UUID | None = None,
    scopes: tuple[uuid.UUID, ...] = (),
) -> uuid.UUID:
    """Store a remembered blob as one working-memory item and return its id, the cheap front write.

    A remember lands here first rather than paying the chunk, embed, and extract pipeline up
    front, so a capture is a single embedded row the recall lane can already rank. The promotion
    pass later feeds the aged or overflow items into the long-term graph. The write runs as
    owner_id so the row level security write policy admits it.

    text: the content to remember.
    kind: coarse type tag carried through to the promoted document.
    owner_id: user that owns the stored item, the system user when null.
    scopes: group set the item is shared with, private to the owner when empty.
    """
    owner_id = owner_id or settings.system_user_id
    [embedding] = await Embedder().embed([text], mode="document")
    async with acting_as(owner_id):
        item = SessionItem(
            kind=kind, text=text, embedding=embedding, owner_id=owner_id, scopes=list(scopes)
        )
        session().add(item)
        await session().flush()
        logger.info("remembered session item {} kind={}", item.id, kind)
        return item.id
