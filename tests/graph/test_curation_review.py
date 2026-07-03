import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import NamedTuple

import pytest
from factories import build_live_fact
from graphdb import (
    FakeLLM,
    add_member,
    add_principals,
    create_group,
    delete_group,
    drop_principals,
    purge_owner,
)

from aizk.config import settings
from aizk.graph.curation_review import (
    curated_groups_administered,
    render_review_prompt,
    review_curated_groups,
    review_group,
    visible_canon,
)
from aizk.graph.models import CurationReview, CurationVerdict
from aizk.store import (
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    Watermark,
    acting_as,
)


class Reviewer(NamedTuple):
    """A curated group with an admin member (the reviewer) and a writer whose claims it judges.

    reviewer: admin member the review pass runs as, the fan-out's own per-principal identity.
    writer: writer member whose claims land pending for the reviewer to judge.
    group: the curated scope both belong to.
    """

    reviewer: uuid.UUID
    writer: uuid.UUID
    group: uuid.UUID


@asynccontextmanager
async def reviewer_brain(*, curated: bool = True) -> AsyncIterator[Reviewer]:
    """Yield a seeded curated group with an admin reviewer and a writer, torn down on exit."""
    reviewer, writer = uuid.uuid4(), uuid.uuid4()
    await add_principals(reviewer, writer)
    group = await create_group(f"review-{uuid.uuid4().hex[:8]}", curated=curated)
    await add_member(reviewer, group, role="admin")
    await add_member(writer, group, role="writer")
    try:
        yield Reviewer(reviewer, writer, group)
    finally:
        await delete_group(group)
        for principal in (reviewer, writer):
            await purge_owner(principal)
        await drop_principals(reviewer, writer)


async def plant_claim(
    owner: uuid.UUID, scope: uuid.UUID | None, statement: str, reviewed_at: datetime | None
) -> uuid.UUID:
    """Insert one entity and a fact claim naming it, stamped with a given reviewed_at.

    owner: principal that owns both claims.
    scope: group the claims are shared with, private when null.
    statement: the fact's natural-language statement, unique per call so ids never collide.
    reviewed_at: the review stamp to seed, null for a pending claim.
    """
    entity_content, entity_claim = uuid.uuid4(), uuid.uuid4()
    fact_content, fact_claim = uuid.uuid4(), uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(EntityContent(id=entity_content, name=statement, type="Concept"))
        await session.flush()
        session.add(
            EntityClaim(id=entity_claim, content_id=entity_content, owner_id=owner, scope=scope)
        )
        session.add(
            FactContent(
                id=fact_content,
                subject_id=entity_content,
                predicate="related_to",
                statement=statement,
            )
        )
        await session.flush()
        session.add(
            FactClaim(
                id=fact_claim,
                content_id=fact_content,
                owner_id=owner,
                scope=scope,
                reviewed_at=reviewed_at,
            )
        )
    return fact_claim


async def claim_state(owner: uuid.UUID, claim_id: uuid.UUID) -> tuple[bool, bool]:
    """Whether a claim still exists and, if so, whether it is reviewed, read as its owner.

    owner: identity the read acts as.
    claim_id: claim to inspect.
    """
    async with acting_as(owner) as session:
        row = await session.get(
            FactClaim, claim_id, execution_options={settings.skip_live_gate: True}
        )
    return row is not None, row is not None and row.reviewed_at is not None


def test_render_review_prompt_lists_the_canon_then_the_pending_claims_by_id() -> None:
    """The rendered prompt names each pending claim's id and statement below the approved canon."""
    claim = build_live_fact(statement="a pending claim")

    rendered = render_review_prompt(["an approved fact"], [claim])

    assert "Approved canon." in rendered
    assert "- an approved fact" in rendered
    assert f"id={claim.id} statement=a pending claim" in rendered


def test_render_review_prompt_names_an_empty_canon_explicitly() -> None:
    """A fresh group with no approved canon yet still renders a legible prompt, never a blank."""
    rendered = render_review_prompt([], [])
    assert "(no approved canon yet)" in rendered


def test_curated_groups_administered_filters_to_admin_role_and_curated_only(
    requires_db: None,
) -> None:
    """Only a curated group where the principal holds the admin role is returned.

    A reader-role membership in the same curated group and an admin-role membership in an
    uncurated one both stay off the roster, so the pass never reviews a group it cannot actually
    curate or one that was never curated in the first place.
    """

    async def probe() -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        async with reviewer_brain() as brain:
            uncurated = await create_group(f"open-{uuid.uuid4().hex[:8]}", curated=False)
            await add_member(brain.reviewer, uncurated, role="admin")
            try:
                administered = await curated_groups_administered(brain.reviewer)
                writer_side = await curated_groups_administered(brain.writer)
                return {g.id for g in administered}, {g.id for g in writer_side}
            finally:
                await delete_group(uncurated)

    administered, writer_side = asyncio.run(probe())
    assert administered  # the curated group the reviewer administers is somewhere in the roster
    assert writer_side == set()  # a writer-role membership never counts as administering


def test_visible_canon_returns_only_reviewed_claims_newest_first(requires_db: None) -> None:
    """The canon carries approved claims only, ordered most-recently-recorded first."""

    async def probe() -> list[str]:
        async with reviewer_brain() as brain:
            await plant_claim(brain.writer, brain.group, "still pending", None)
            await plant_claim(brain.writer, brain.group, "older approved", datetime.now(UTC))
            await plant_claim(brain.writer, brain.group, "newer approved", datetime.now(UTC))
            async with acting_as(brain.reviewer) as session:
                group = (await curated_groups_administered(brain.reviewer))[0]
                return await visible_canon(session, group)

    canon = asyncio.run(probe())
    assert "still pending" not in canon
    assert canon.index("newer approved") < canon.index("older approved")


def test_review_group_skips_when_no_pending_claims_exist(requires_db: None) -> None:
    """A group with an empty queue is skipped outright, no judge call and no watermark write."""

    async def probe() -> tuple[int, int]:
        async with reviewer_brain() as brain:
            group = (await curated_groups_administered(brain.reviewer))[0]
            return await review_group(brain.reviewer, group)

    assert asyncio.run(probe()) == (0, 0)


@pytest.mark.usefixtures("fake_embedder")
def test_review_group_approves_and_rejects_per_verdict_and_updates_the_watermark(
    requires_db: None, fake_llm: FakeLLM
) -> None:
    """Each pending claim is approved or rejected per its verdict, and the watermark advances."""

    async def probe() -> tuple[bool, bool, bool, bool, int]:
        async with reviewer_brain() as brain:
            keep = await plant_claim(brain.writer, brain.group, "a solid claim", None)
            drop = await plant_claim(brain.writer, brain.group, "a shaky claim", None)
            fake_llm.completions.responses[CurationReview] = CurationReview(
                verdicts=[
                    CurationVerdict(claim=keep, approve=True, reason="consistent with canon"),
                    CurationVerdict(claim=drop, approve=False, reason="unsupported"),
                ]
            )

            approved, rejected = await review_group(
                brain.reviewer, (await curated_groups_administered(brain.reviewer))[0]
            )

            keep_exists, keep_reviewed = await claim_state(brain.writer, keep)
            drop_exists, _ = await claim_state(brain.writer, drop)
            async with acting_as(brain.reviewer) as session:
                watermark = await Watermark.read(
                    session,
                    brain.reviewer,
                    Watermark.Kind.curation_pending,
                    ref=str(brain.group),
                )
            return (
                keep_exists,
                keep_reviewed,
                drop_exists,
                (approved, rejected) == (1, 1),
                watermark,
            )

    keep_exists, keep_reviewed, drop_exists, counted, watermark = asyncio.run(probe())
    assert keep_exists is True and keep_reviewed is True
    assert drop_exists is False
    assert counted is True
    assert watermark == 2


@pytest.mark.usefixtures("fake_embedder")
def test_review_group_is_debounced_once_the_pending_count_repeats(
    requires_db: None, fake_llm: FakeLLM
) -> None:
    """A queue sitting at the same pending count as the last pass is skipped, no repeat judge call.

    The seeded verdict deliberately names a claim id outside the pending set, so the one real
    pending claim never resolves and the queue count stays put between the two calls, the case
    that tells debounce-on-count apart from the trivially skipped empty queue.
    """

    async def probe() -> tuple[tuple[int, int], tuple[int, int], int]:
        async with reviewer_brain() as brain:
            await plant_claim(brain.writer, brain.group, "never matched by a verdict", None)
            fake_llm.completions.responses[CurationReview] = CurationReview(
                verdicts=[CurationVerdict(claim=uuid.uuid4(), approve=True, reason="off-target")]
            )
            group = (await curated_groups_administered(brain.reviewer))[0]

            first = await review_group(brain.reviewer, group)
            calls_before = len(fake_llm.completions.calls)
            second = await review_group(brain.reviewer, group)
            calls_after = len(fake_llm.completions.calls)
            return first, second, calls_after - calls_before

    first, second, extra_calls = asyncio.run(probe())
    assert first == (0, 0)  # the off-target verdict matched nothing, so nothing was resolved
    assert second == (0, 0)  # debounced this time, the pending count sat unchanged
    assert extra_calls == 0  # the debounced run never re-asked the model


def test_review_curated_groups_aggregates_the_judged_count_across_every_group(
    requires_db: None,
) -> None:
    """With nothing pending anywhere the aggregate across every administered group is zero."""

    async def probe() -> int:
        async with reviewer_brain() as brain:
            return await review_curated_groups(brain.reviewer)

    assert asyncio.run(probe()) == 0
