import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import NamedTuple

import dbutil
import pytest
from factories import build_live_fact
from sqlalchemy import text

from aizk.config import settings
from aizk.graph.curation_review import (
    curated_groups_administered,
    render_review_prompt,
    review_curated_groups,
    review_group,
    visible_canon,
)
from aizk.graph.models import CurationReview, CurationVerdict
from aizk.store import Group, Watermark, acting_as, system_session


class Brain(NamedTuple):
    """A curated group with an admin reviewer and a writer whose claims it judges."""

    reviewer: uuid.UUID
    writer: uuid.UUID
    group: uuid.UUID


@pytest.fixture
def brain(migrated_db: None) -> Iterator[Brain]:
    """A reset schema seeding the system principal, a curated group, its admin, and one writer."""
    reviewer, writer, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def setup() -> None:
        await dbutil.reset_db()
        await dbutil.seed_principal(settings.system_principal_id, is_admin=True)
        await dbutil.seed_principal(reviewer)
        await dbutil.seed_principal(writer)
        await dbutil.seed_group(group, name=f"review-{group}", curated=True)
        await dbutil.seed_membership(reviewer, group, "admin")
        await dbutil.seed_membership(writer, group, "writer")

    dbutil.run(setup())
    yield Brain(reviewer, writer, group)


async def plant_claim(
    owner: uuid.UUID, group: uuid.UUID, statement: str, reviewed: datetime | None
) -> uuid.UUID:
    """Seed one entity and a fact claim in a group's scope, stamped reviewed or left pending."""
    entity, content, claim = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await dbutil.admin_exec(
        "INSERT INTO entity_content (id, name, type) VALUES (:id, :name, 'Concept')",
        {"id": entity, "name": statement},
    )
    await dbutil.admin_exec(
        "INSERT INTO fact_content (id, subject_id, predicate, statement) "
        "VALUES (:id, :subject, 'related_to', :statement)",
        {"id": content, "subject": entity, "statement": statement},
    )
    await dbutil.admin_exec(
        "INSERT INTO fact_claim (id, content_id, owner_id, scopes, reviewed_at) "
        "VALUES (:id, :content, :owner, CAST(:scopes AS uuid[]), :reviewed)",
        {
            "id": claim,
            "content": content,
            "owner": owner,
            "scopes": [str(group)],
            "reviewed": reviewed,
        },
    )
    return claim


async def claim_state(claim_id: uuid.UUID) -> tuple[bool, bool]:
    """Whether a claim still exists and, if so, whether it is reviewed, read past every gate."""
    async with dbutil.admin_engine().connect() as connection:
        result = await connection.execute(
            text("SELECT reviewed_at FROM fact_claim WHERE id = :id"), {"id": claim_id}
        )
        row = result.first()
    return row is not None, row is not None and row[0] is not None


@pytest.mark.parametrize(
    ("canon", "pending_statements", "expected"),
    [
        (["an approved fact"], ["a pending claim"], "id="),
        ([], [], "(no approved canon yet)"),
    ],
)
def test_render_review_prompt_lists_canon_then_pending_by_id(
    canon: list[str], pending_statements: list[str], expected: str
) -> None:
    """The prompt names the approved canon, then each pending claim by id, never a blank canon."""
    pending = [build_live_fact(statement=stmt) for stmt in pending_statements]
    rendered = render_review_prompt(canon, pending)
    assert "Approved canon." in rendered and "Pending claims." in rendered
    assert expected in rendered
    for claim in pending:
        assert f"id={claim.id} statement={claim.statement}" in rendered


def test_curated_groups_administered_filters_to_admin_and_curated(brain: Brain) -> None:
    """Only a curated group the principal admins is returned, a reader or writer role excluded.

    An admin role in an uncurated group and a reader role in another curated group both stay off
    the roster, and the writer never administers anything, so the pass reviews only what it can.
    """

    async def probe() -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        uncurated, other = uuid.uuid4(), uuid.uuid4()
        await dbutil.seed_group(uncurated, name=f"open-{uncurated}", curated=False)
        await dbutil.seed_group(other, name=f"canon-{other}", curated=True)
        await dbutil.seed_membership(brain.reviewer, uncurated, "admin")
        await dbutil.seed_membership(brain.reviewer, other, "reader")
        administered = await curated_groups_administered(brain.reviewer)
        writer_side = await curated_groups_administered(brain.writer)
        return {g.id for g in administered}, {g.id for g in writer_side}

    administered, writer_side = dbutil.run(probe())
    assert administered == {brain.group}
    assert writer_side == set()


def test_visible_canon_returns_only_reviewed_claims_newest_first(brain: Brain) -> None:
    """The canon carries approved claims only, ordered most-recently-recorded first."""

    async def probe() -> list[str]:
        await plant_claim(brain.writer, brain.group, "still pending", None)
        await plant_claim(brain.writer, brain.group, "older approved", datetime.now(UTC))
        await plant_claim(brain.writer, brain.group, "newer approved", datetime.now(UTC))
        async with system_session() as session:
            group = await session.get(Group, brain.group)
            assert group is not None
            return await visible_canon(session, group)

    canon = dbutil.run(probe())
    assert "still pending" not in canon
    assert canon.index("newer approved") < canon.index("older approved")


def test_review_group_skips_an_empty_queue(brain: Brain) -> None:
    """A group with no pending claim is skipped outright, no judge call and no watermark write."""

    async def probe() -> tuple[int, int]:
        group = (await curated_groups_administered(brain.reviewer))[0]
        return await review_group(brain.reviewer, group)

    assert dbutil.run(probe()) == (0, 0)


@pytest.mark.usefixtures("fake_embedder")
def test_review_group_approves_rejects_and_advances_the_watermark(
    brain: Brain, fake_llm: object
) -> None:
    """Each pending claim is approved or rejected per its verdict, and the watermark advances."""

    async def probe() -> tuple[bool, bool, bool, tuple[int, int], int]:
        keep = await plant_claim(brain.writer, brain.group, "a solid claim", None)
        drop = await plant_claim(brain.writer, brain.group, "a shaky claim", None)
        fake_llm.register(
            CurationReview,
            CurationReview(
                verdicts=[
                    CurationVerdict(claim=keep, approve=True, reason="consistent with canon"),
                    CurationVerdict(claim=drop, approve=False, reason="unsupported"),
                ]
            ),
        )
        group = (await curated_groups_administered(brain.reviewer))[0]
        counts = await review_group(brain.reviewer, group)
        keep_exists, keep_reviewed = await claim_state(keep)
        drop_exists, _ = await claim_state(drop)
        async with acting_as(brain.reviewer) as session:
            watermark = await Watermark.read(
                session, brain.reviewer, Watermark.Kind.curation_pending, ref=str(brain.group)
            )
        return keep_exists, keep_reviewed, drop_exists, counts, watermark

    keep_exists, keep_reviewed, drop_exists, counts, watermark = dbutil.run(probe())
    assert keep_exists and keep_reviewed
    assert drop_exists is False
    assert counts == (1, 1)
    assert watermark == 2


@pytest.mark.usefixtures("fake_embedder")
def test_review_group_is_debounced_once_the_pending_count_repeats(
    brain: Brain, fake_llm: object
) -> None:
    """A queue sitting at the same pending count as the last pass is skipped, no repeat judge call.

    The verdict names a claim id outside the pending set, so the one real pending claim never
    resolves and the count stays put between passes, telling debounce-on-count apart from the
    trivially skipped empty queue.
    """

    async def probe() -> tuple[tuple[int, int], tuple[int, int], int]:
        await plant_claim(brain.writer, brain.group, "never matched by a verdict", None)
        fake_llm.register(
            CurationReview,
            CurationReview(
                verdicts=[CurationVerdict(claim=uuid.uuid4(), approve=True, reason="off-target")]
            ),
        )
        group = (await curated_groups_administered(brain.reviewer))[0]
        first = await review_group(brain.reviewer, group)
        before = len(fake_llm.completions.calls)
        second = await review_group(brain.reviewer, group)
        after = len(fake_llm.completions.calls)
        return first, second, after - before

    first, second, extra_calls = dbutil.run(probe())
    assert first == (0, 0)  # the off-target verdict matched nothing, so nothing resolved
    assert second == (0, 0)  # debounced this time, the pending count sat unchanged
    assert extra_calls == 0  # the debounced run never re-asked the model


def test_review_curated_groups_aggregates_across_every_group(brain: Brain) -> None:
    """With nothing pending anywhere the aggregate across every administered group is zero."""
    assert dbutil.run(review_curated_groups(brain.reviewer)) == 0
