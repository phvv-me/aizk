import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Index, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, Relationship

from ...mixins import Id, Scoped, TableBase, Timestamped

if TYPE_CHECKING:
    from .chunk import Chunk


class Document(Id, Scoped, Timestamped, TableBase, table=True):
    """A source item ingested into memory, parent of its ordered chunks.

    id: stable identity, generated client-side on insert.
    kind: coarse type tag such as note, file, or message.
    title: human-readable label when one is known.
    source_uri: unique origin locator used to dedupe re-ingestion.
    content_hash: digest of the source content for change detection.
    owner_id: user that owns the row, enforced by row level security.
    scopes: group set the row is shared with, an implicit intersection when it names more than
        one, empty when private to the owner.
    promoted_from: source document this row was copied from when it was promoted to a wider scope,
        null for an original, the provenance link that keeps a promotion auditable and one-way.
    created_at: first-seen timestamp.
    updated_at: last-write timestamp.
    chunks: ordered text spans embedded for hybrid search.
    """

    # scopes earns its own GIN index here like chunk's, since promotion copies and RLS reads both
    # filter documents by target scope-set often; a table arg rather than a redeclared mixin Field
    # so pydantic never sees the child shadowing Scoped.scopes
    __table_args__ = (Index("ix_document_scopes", "scopes", postgresql_using="gin"),)

    kind: str = Field(default="note")
    title: str | None = Field(default=None)
    source_uri: str | None = Field(default=None, unique=True)
    content_hash: str = Field(index=True)
    promoted_from: uuid.UUID | None = Field(default=None, foreign_key="document.id")

    # no back_populates: Chunk carries no `document` relationship of its own, every read site
    # already id-keyed rather than navigating from a loaded Chunk, while `Document.chunks` itself
    # stays, `graph/promote.py`'s document-copy path actually loads and iterates it.
    # cascade_delete=True/passive_deletes=True replace the hand-rolled "all, delete-orphan" cascade
    # plus its passive_deletes kwarg, both first-class Relationship kwargs; order_by has no such
    # first-class kwarg, so it stays in sa_relationship_kwargs alone.
    chunks: list[Chunk] = Relationship(
        cascade_delete=True,
        passive_deletes=True,
        sa_relationship_kwargs={"order_by": "Chunk.ord"},
    )

    @classmethod
    async def move_to_scope(
        cls,
        session: AsyncSession,
        owner_id: uuid.UUID,
        document_ids: list[uuid.UUID],
        scopes: tuple[uuid.UUID, ...],
    ) -> int:
        """Re-scope the caller's own documents, their chunks, and the facts mined from them.

        The coherent unit of a note travels together: the source rows and the claims extracted from
        them all follow it, walking `fact_claim.source_chunk_id -> chunk.document_id`, the same
        provenance chain `forget` uses. Only rows the caller owns move, an explicit `owner_id`
        filter beneath whatever the caller's own session already permits, so a writer in a shared
        group cannot re-scope another member's contribution. Scope is an access label rather than a
        bi-temporal fact, so this is an in-place re-scope, not a supersession. Runs on the caller's
        own session, so row level security also refuses any source row they may not write, and the
        target set is validated by the caller before the call. Returns how many documents moved.

        session: the caller's own acting session.
        owner_id: the caller, whose rows alone move.
        document_ids: the documents to move.
        scopes: the sorted target group-id set, empty to make them private again.
        """
        from .chunk import Chunk
        from .fact import FactClaim

        target = list(scopes)
        chunk_ids = select(Chunk.id).where(Chunk.document_id.in_(document_ids))
        await session.execute(
            update(FactClaim)
            .where(FactClaim.source_chunk_id.in_(chunk_ids), FactClaim.owner_id == owner_id)
            .values(scopes=target)
        )
        await session.execute(
            update(Chunk)
            .where(Chunk.document_id.in_(document_ids), Chunk.owner_id == owner_id)
            .values(scopes=target)
        )
        moved = await session.execute(
            update(cls)
            .where(cls.id.in_(document_ids), cls.owner_id == owner_id)
            .values(scopes=target)
        )
        return moved.rowcount
