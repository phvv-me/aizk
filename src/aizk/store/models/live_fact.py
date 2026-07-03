import uuid
from datetime import datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Column, DateTime, Integer, Table, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, Range

from ...config import settings
from ..mixins import TableBase, aizk_registry

# live_fact joins fact_claim to its fact_content and narrows to exactly the live version, so this
# table carries a merged column set neither side alone has: fc.id and fc.content_id name both
# halves of the join explicitly (the "expose both" contract `promote`, `curation`, and the recall
# lanes all read), the structural columns (subject_id, object_id, predicate, statement, embedding)
# come from the deduplicated content, and every bi-temporal, curation, and decay column
# (owner_id, scope, valid, recorded, reviewed_at, last_accessed, access_count, attributes,
# source_chunk_id, promoted_from) comes from the claim. A hand-built `Table` rather than a
# `to_metadata` copy, since this view's columns merge two different tables rather than narrowing
# one; `info={"is_view": True}` keeps it out of the alembic autogenerate diff surface, the signal
# `env.py`'s `include_object` and the reflected-view name check both key off.
live_fact_table = Table(
    "live_fact",
    TableBase.metadata,
    Column("id", Uuid(), primary_key=True),
    Column("content_id", Uuid()),
    Column("subject_id", Uuid()),
    Column("object_id", Uuid()),
    Column("predicate", Text()),
    Column("statement", Text()),
    Column("embedding", HALFVEC(settings.embed_dim)),
    Column("owner_id", Uuid()),
    Column("scope", Uuid()),
    Column("valid", TSTZRANGE()),
    Column("recorded", TSTZRANGE()),
    Column("reviewed_at", DateTime(timezone=True)),
    Column("last_accessed", DateTime(timezone=True)),
    Column("access_count", Integer()),
    Column("attributes", JSONB()),
    Column("source_chunk_id", Uuid()),
    Column("promoted_from", Uuid()),
    info={"is_view": True},
)


class LiveFact:
    """A read-only mirror of the live `fact_claim` x `fact_content` join, mapped onto `live_fact`.

    The view is `fact_claim JOIN fact_content WHERE upper_inf(recorded) AND (valid IS NULL OR
    valid @> now())`, exactly `FactClaim.is_current`, so a caller that only ever wants the live
    graph reads through this class instead of re-deriving the same temporal predicate and the same
    claim-to-content join by hand on every query. `security_invoker = true` on the view means every
    read still runs under the calling session's own row level security, not the view owner's, so
    switching a query from `FactClaim`/`FactContent` to `LiveFact` narrows which rows are live
    without widening which rows are visible. Never written to: every write still targets
    `FactContent` and `FactClaim` and the base tables they map.

    The fields below are plain type annotations, not SQLModel `Field`s, mapped imperatively rather
    than declaratively since `LiveFact` carries no metaclass of its own; `map_imperatively` below
    instruments the real attributes from `live_fact_table`'s columns at import time regardless of
    what is annotated here.

    id: the live claim's own identity.
    content_id: the fact content this claim stakes, the structural row two containers may share.
    subject_id: entity content the fact is about.
    object_id: entity content the fact points to, null for unary facts.
    predicate: ontology relation type.
    statement: self-contained natural-language rendering of the fact.
    embedding: halfvec dense vector of the statement, null until embedded.
    owner_id: principal that holds this claim.
    scope: group this claim is shared with, null when private to the owner.
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
    scope: uuid.UUID | None
    valid: Range[datetime] | None
    recorded: Range[datetime]
    reviewed_at: datetime | None
    last_accessed: datetime | None
    access_count: int
    attributes: dict
    source_chunk_id: uuid.UUID | None
    promoted_from: uuid.UUID | None


aizk_registry.map_imperatively(LiveFact, live_fact_table)
