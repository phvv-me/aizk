import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import Range, insert
from sqlalchemy.exc import IntegrityError

from ..store import EntityClaim, FactClaim
from ..store.context import session
from ..store.mixins import TableBase

# the value types a caller stakes an optional `FactClaim` column with (`valid`, `source_chunk_id`,
# `reviewed_at`, `attributes`, `promoted_from`); `dict` bare mirrors `FactClaim.attributes`'s own
# field type, the JSONB column's genuinely heterogeneous value shape. `| None` covers a column
# a caller resolved to explicitly null (`valid`, `reviewed_at`), as opposed to one it never
# passes at all, which claim_fact's **kwargs simply omits from the insert so the column's own
# server_default, not an explicit NULL, applies.
type ClaimField = Range[datetime] | datetime | uuid.UUID | dict | None

# the partial live-uniqueness `fact_claim` carries, one open-`recorded` claim per (content, owner,
# scopes); ON CONFLICT DO NOTHING against it is what makes `claim_fact`'s insert idempotent under
# a concurrent durable worker re-processing the same chunk, the same statements whichever writer
# gets there first rather than a unique-violation crash.
FACT_CLAIM_LIVE_ARBITER = {
    "index_elements": ["content_id", "owner_id", "scopes"],
    "index_where": text("upper_inf(recorded)"),
}


async def mint_content(content: TableBase) -> None:
    """Insert one content row, tolerating a duplicate another tenant already minted.

    Content is visible only through a claim (`store.models.tables.entity.content_policies`), so
    Postgres refuses to even plan a plain `INSERT ... ON CONFLICT` against it. Row level security
    requires the acting role to hold unconditional SELECT visibility into whatever row might
    conflict before it will plan an `ON CONFLICT` clause at all, DO NOTHING included, a hidden
    conflict can never resolve silently, the very existence side channel this content/claim split
    exists to close from the other direction. A savepoint is the idiom that stays compatible with
    that Postgres restriction. Attempt the insert inside a nested transaction, and a
    `UniqueViolation` on the content's own content-addressed primary key rolls back only the
    savepoint, so the surrounding write proceeds identically whether this exact content already
    existed or not, with no crash and no observable difference between the two cases either way.

    content: the transient content row to insert, its primary key already content-addressed.
    """
    try:
        async with session().begin_nested():
            session().add(content)
            await session().flush()
    except IntegrityError:
        pass


async def claim_entity(
    content_id: uuid.UUID, owner_id: uuid.UUID, scopes: list[uuid.UUID]
) -> None:
    """Idempotently insert one entity claim, a no-op when (content, owner, scopes) already exists.

    content_id: entity content this claim stakes.
    owner_id: user the new claim is staked under.
    scopes: group set the new claim is shared with, private when empty.
    """
    await session().execute(
        insert(EntityClaim)
        .values(content_id=content_id, owner_id=owner_id, scopes=scopes)
        .on_conflict_do_nothing(index_elements=["content_id", "owner_id", "scopes"])
    )


async def claim_fact(
    content_id: uuid.UUID,
    owner_id: uuid.UUID,
    scopes: list[uuid.UUID],
    **claim_fields: ClaimField,
) -> None:
    """Idempotently insert one fact claim, a no-op against an identical already-live claim.

    content_id: fact content this claim stakes.
    owner_id: user the new claim is staked under.
    scopes: group set the new claim is shared with, private when empty.
    claim_fields: further `FactClaim` columns the caller already resolved (`valid`,
        `source_chunk_id`, `reviewed_at`, `attributes`, `promoted_from`, ...).
    """
    await session().execute(
        insert(FactClaim)
        .values(content_id=content_id, owner_id=owner_id, scopes=scopes, **claim_fields)
        .on_conflict_do_nothing(**FACT_CLAIM_LIVE_ARBITER)
    )
