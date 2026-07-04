import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..extract.llm import structured
from ..store import Group, LiveFact, Membership, Watermark, acting_as, system_session
from .models import CurationReview


async def curated_groups_administered(principal_id: uuid.UUID) -> list[Group]:
    """The curated groups (groups whose writes need admin review before they count as approved
    canon) this principal holds the admin membership role in.

    `Membership` and `Group` carry no row level security of their own, so this reads the same
    under this principal's own session as it would under any other. The review pass's own
    elevated reach into each group's pending queue and canon is scoped separately per group in
    `review_group`, never here.

    principal_id: identity whose admin memberships are read.
    """
    async with acting_as(principal_id) as session:
        group_ids = list(
            await session.scalars(
                select(Membership.group_id).where(
                    Membership.principal_id == principal_id,
                    Membership.role == Membership.Role.admin,
                )
            )
        )
        groups: list[Group] = []
        for group_id in group_ids:
            group = await session.get(Group, group_id)
            if group is not None and group.curated:
                groups.append(group)
    return groups


async def visible_canon(session: AsyncSession, group: Group) -> list[str]:
    """The group's already-approved claim statements, the only material a verdict may ground in.

    `LiveFact` reads bypass the ordinary pending-claim gate entirely, so the reviewed_at filter is
    listed explicitly here rather than relied on, the mirror image of `Group.pending_facts`'s own
    explicit null filter for the opposite half of the queue.

    session: open session, already acting as the system principal.
    group: the curated group whose canon grounds the judgment.
    """
    return list(
        await session.scalars(
            select(LiveFact.statement)
            .where(LiveFact.scopes.contains([group.id]), LiveFact.reviewed_at.is_not(None))
            .order_by(LiveFact.recorded.desc())
            .limit(settings.curation_review_canon_k)
        )
    )


def render_review_prompt(canon: list[str], pending: list[LiveFact]) -> str:
    """Render the approved canon and the pending queue as the judge's structured-call user turn.

    canon: the group's already-approved claim statements grounding the judgment.
    pending: the pending claims awaiting a verdict, each shown with its id and statement.
    """
    canon_block = "\n".join(f"- {statement}" for statement in canon) or "(no approved canon yet)"
    pending_block = "\n".join(f"- id={claim.id} statement={claim.statement}" for claim in pending)
    return f"Approved canon.\n{canon_block}\n\nPending claims.\n{pending_block}"


async def judge_pending(canon: list[str], pending: list[LiveFact]) -> CurationReview:
    """Ask the LLM for one approve-or-reject verdict per pending claim, grounded in the canon.

    canon: the group's already-approved claim statements.
    pending: the pending claims to judge.
    """
    return await structured(
        settings.curation_review_system, render_review_prompt(canon, pending), CurationReview
    )


def sort_verdicts(
    pending: list[LiveFact], review: CurationReview
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Split the pending queue into approved and rejected claim ids, from the judge's own verdicts.

    pending: the pending claims that were judged.
    review: the judge's per-claim verdicts, echoing back the claim id each judges.
    """
    verdicts = {verdict.claim: verdict.approve for verdict in review.verdicts}
    approved = [claim.id for claim in pending if verdicts.get(claim.id) is True]
    rejected = [claim.id for claim in pending if verdicts.get(claim.id) is False]
    return approved, rejected


async def debounced(principal_id: uuid.UUID, group: Group, pending_count: int) -> bool:
    """Whether the pending queue is empty or unchanged since the last review, the pass's skip
    signal.

    Unlike the total fact count `CommunitiesTask`/`RaptorTask` gate on, which only ever grows, a
    pending queue also shrinks as claims are judged, so the skip condition is an unchanged count
    since the last pass, catching both a still-empty queue and one unchanged since last time.

    principal_id: the admin member this pass reviews on behalf of, who owns the watermark, the
        stored row recording last time's pending count.
    group: the curated group being reviewed.
    pending_count: how many claims are pending right now.
    """
    async with acting_as(principal_id) as session:
        last = await Watermark.read(
            session, principal_id, Watermark.Kind.curation_pending, ref=str(group.id)
        )
    return pending_count == 0 or pending_count == last


async def apply_verdicts(
    group_id: uuid.UUID, approved: list[uuid.UUID], rejected: list[uuid.UUID]
) -> None:
    """Approve and reject the judged claims, on the system-elevated session a curated write needs.

    A still-pending claim is invisible to every reader but its own author under the ordinary
    curation gate, so the read that found it and the write that resolves it both run under the
    system principal's elevated reach. `curated_groups_administered` already vetted this pass's own
    principal as the group's own admin member before this ever runs.

    group_id: the curated group whose queue is being resolved.
    approved: claim ids the judge approved.
    rejected: claim ids the judge rejected.
    """
    async with system_session() as session:
        group_row = await session.get(Group, group_id)
        assert group_row is not None  # vetted moments ago by curated_groups_administered
        if approved:
            await group_row.approve_facts(session, approved)
        if rejected:
            await group_row.reject_facts(session, rejected)


async def review_group(principal_id: uuid.UUID, group: Group) -> tuple[int, int]:
    """Judge one curated group's pending queue and approve or reject each claim, return the count.

    The watermark itself is this principal's own private bookkeeping row, so it is read and
    written under this principal's own session, the scope its write policy's WITH CHECK admits.

    principal_id: the admin member this pass reviews on behalf of, the watermark's own owner.
    group: the curated group to review.
    """
    async with system_session() as session:
        pending = await group.pending_facts(session)
    if await debounced(principal_id, group, len(pending)):
        logger.info(
            "curation review skipped for group {}, {} still pending", group.id, len(pending)
        )
        return 0, 0
    async with system_session() as session:
        canon = await visible_canon(session, group)
    approved, rejected = sort_verdicts(pending, await judge_pending(canon, pending))
    await apply_verdicts(group.id, approved, rejected)
    async with acting_as(principal_id) as session:
        await Watermark.set_value(
            session,
            principal_id,
            Watermark.Kind.curation_pending,
            counter=len(pending),
            ref=str(group.id),
        )
    logger.info(
        "curation review for group {} approved {} and rejected {} of {} pending",
        group.id,
        len(approved),
        len(rejected),
        len(pending),
    )
    return len(approved), len(rejected)


async def review_curated_groups(principal_id: uuid.UUID) -> int:
    """Review every curated group this principal administers, return the total claims judged.

    principal_id: the admin member whose curated groups are reviewed.
    """
    groups = await curated_groups_administered(principal_id)
    judged = 0
    for group in groups:
        approved, rejected = await review_group(principal_id, group)
        judged += approved + rejected
    return judged
