import uuid
from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import Range

from ...mixins import ViewBase
from ..tables.fact import FactClaim, FactContent


class LiveFact(ViewBase):
    """A read-only mirror of the live `fact_claim` x `fact_content` join, mapped onto `live_fact`.

    The view is `fact_claim JOIN fact_content WHERE FactClaim.is_current`, so a caller that only
    ever wants the live graph reads through this class instead of re-deriving the same temporal
    predicate and the same claim-to-content join by hand on every query. `__view_select__` below
    is that predicate's one SQL rendering, the source both the mapped columns and the migration's
    `CREATE VIEW` DDL (`store.mixins.view.create_view_ddl`) are built from. `security_invoker =
    true` on the view means every read still runs under the calling session's own row level
    security, not the view owner's, so switching a query from `FactClaim`/`FactContent` to
    `LiveFact` narrows which rows are live without widening which rows are visible. Never written
    to: every write still targets `FactContent` and `FactClaim` and the base tables they map.

    id: the live claim's own identity.
    content_id: the fact content this claim stakes, the structural row two containers may share.
    subject_id: entity content the fact is about.
    object_id: entity content the fact points to, null for unary facts.
    predicate: ontology relation type.
    statement: self-contained natural-language rendering of the fact.
    embedding: halfvec dense vector of the statement, null until embedded.
    owner_id: user that holds this claim.
    scopes: group set this claim is shared with, empty when private to the owner.
    valid: world-time range when the statement holds.
    recorded: transaction-time range, always open on every row this view admits.
    reviewed_at: when this claim cleared curated-group review, null while still pending.
    last_accessed: transaction time recall last surfaced this claim.
    access_count: how many times recall has surfaced this claim.
    attributes: free-form structured detail extracted alongside this claim.
    source_chunk_id: chunk the fact was extracted from, null when the chunk is gone.
    promoted_from: the claim this one was promoted from, null for an ordinary write.
    """

    id: uuid.UUID
    content_id: uuid.UUID
    subject_id: uuid.UUID
    object_id: uuid.UUID | None
    predicate: str
    statement: str
    embedding: list[float] | None
    owner_id: uuid.UUID
    scopes: list[uuid.UUID]
    valid: Range[datetime] | None
    recorded: Range[datetime]
    reviewed_at: datetime | None
    last_accessed: datetime | None
    access_count: int
    attributes: dict
    source_chunk_id: uuid.UUID | None
    promoted_from: uuid.UUID | None

    @classmethod
    def __view_select__(cls) -> Select:
        """`fact_claim` joined to its `fact_content`, narrowed to `FactClaim.is_current`.

        `fc.id`/`fc.content_id` name both halves of the join explicitly (the "expose both"
        contract `promote`, `curation`, and the recall lanes all read), the structural columns
        (subject_id, object_id, predicate, statement, embedding) come from the deduplicated
        content, and every bi-temporal, curation, and decay column (owner_id, scopes, valid,
        recorded, reviewed_at, last_accessed, access_count, attributes, source_chunk_id,
        promoted_from) comes from the claim. `FactClaim.is_current` is the identical hybrid
        predicate `is_current_expression`, the do_orm_execute loader-criteria listener, and
        `visible_at`'s live branch all share, so this view can never drift from what "current"
        means anywhere else in the schema.
        """
        claim = FactClaim.__table__
        content = FactContent.__table__
        return (
            select(
                claim.c.id.label("id"),
                claim.c.content_id.label("content_id"),
                content.c.subject_id.label("subject_id"),
                content.c.object_id.label("object_id"),
                content.c.predicate.label("predicate"),
                content.c.statement.label("statement"),
                content.c.embedding.label("embedding"),
                claim.c.owner_id.label("owner_id"),
                claim.c.scopes.label("scopes"),
                claim.c.valid.label("valid"),
                claim.c.recorded.label("recorded"),
                claim.c.reviewed_at.label("reviewed_at"),
                claim.c.last_accessed.label("last_accessed"),
                claim.c.access_count.label("access_count"),
                claim.c.attributes.label("attributes"),
                claim.c.source_chunk_id.label("source_chunk_id"),
                claim.c.promoted_from.label("promoted_from"),
            )
            .select_from(claim.join(content, content.c.id == claim.c.content_id))
            .where(FactClaim.is_current)
        )
