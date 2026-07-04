import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from typing import Protocol, runtime_checkable

from loguru import logger
from mainboard.profiling import span
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..graph.communities import community_search
from ..graph.raptor import raptor_search
from ..serving import Embedder
from ..store import FactClaim, acting_as
from .models import CommunityNote, LaneResult, RaptorNote, RecallContext, RecallResult
from .query_route import QueryRoute
from .recall import Recall, has_evidence_gap, session_hits, stage, top_profile


@runtime_checkable
class Lane(Protocol):
    """One independent slice of a recall, run on its own pooled session and fused afterward.

    Stateless value objects, one instance per kind is enough, mirroring `lote`'s scheduler
    registry. A lane reads whatever it needs off `RecallContext` and returns its own `LaneResult`
    slice, never anyone else's.
    """

    async def run(self, session: AsyncSession, ctx: RecallContext) -> LaneResult: ...


class CoreLane:
    """The chunk-and-fact round every recall needs, assembling, gap-filling when thin, and
    recording access."""

    async def run(self, session: AsyncSession, ctx: RecallContext) -> LaneResult:
        round_ = Recall(session, Embedder(), ctx.query, ctx.vector, ctx.k, ctx.as_of, ctx.ppr_on)
        hits, facts = await round_.assemble_context()
        if settings.recall_gap_fill and await has_evidence_gap(ctx.query, hits, facts):
            hits, facts = await round_.fill_gap(hits, facts)
        await FactClaim.record_access(session, [fact.statement for fact in facts])
        return LaneResult(hits=hits, facts=facts)


class SessionLane:
    """The still-working session-item lane, empty when session_recall_k is off."""

    async def run(self, session: AsyncSession, ctx: RecallContext) -> LaneResult:
        if not settings.session_recall_k:
            return LaneResult()
        async with stage("session_hits"):
            notes = await session_hits(session, ctx.vector, settings.session_recall_k)
        return LaneResult(session=notes)


class CommunityLane:
    """The global community-summary lane, empty for a pointed rather than thematic query."""

    async def run(self, session: AsyncSession, ctx: RecallContext) -> LaneResult:
        if not ctx.thematic:
            return LaneResult()
        async with stage("community_search"):
            rows = await community_search(session, ctx.vector, k=3)
        notes = [
            CommunityNote(label=label, summary=summary, score=sc) for label, summary, sc in rows
        ]
        return LaneResult(communities=notes)


class RaptorLane:
    """The RAPTOR summary-tree lane, root summaries when thematic and leaf summaries otherwise."""

    async def run(self, session: AsyncSession, ctx: RecallContext) -> LaneResult:
        if not ctx.raptor_on:
            return LaneResult()
        async with stage("raptor_search"):
            rows = await raptor_search(
                session, ctx.query, ctx.vector, thematic=ctx.thematic, k=settings.raptor_k
            )
        notes = [
            RaptorNote(label=label, summary=summary, level=level, score=sc)
            for label, summary, level, sc in rows
        ]
        return LaneResult(raptor=notes)


class ProfileLane:
    """The top matched entity's rolled-up profile lane, null when profiles is off."""

    async def run(self, session: AsyncSession, ctx: RecallContext) -> LaneResult:
        if not settings.profiles:
            return LaneResult()
        async with stage("top_profile"):
            return LaneResult(profile=await top_profile(session, ctx.vector))


# the fixed lane roster every recall fans out to, one stateless instance per kind; a lane whose
# own gate is off returns an empty LaneResult rather than being excluded from the roster, so the
# roster itself never changes and `gather_lanes` stays a single generic fan-out.
LANES: tuple[Lane, ...] = (CoreLane(), SessionLane(), CommunityLane(), RaptorLane(), ProfileLane())


async def run_lane[T](
    principal_id: uuid.UUID,
    scopes: tuple[uuid.UUID, ...],
    body: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Run one recall lane body on its own pooled session acting as principal_id, scoped to scopes.

    A single `AsyncSession` cannot run two statements at once, so recall's independent lanes each
    check out their own connection from the pool and run concurrently under `asyncio.gather`
    rather than serializing behind one shared session. Cheap only because the app-role engine
    pools real connections. A fresh `NullPool` connection per lane would have traded one latency
    tax for five.

    principal_id: identity whose row level security visibility the lane's session acts under.
    scopes: group ids narrowing the lane's session to that combination's composed graph, the whole
        visible union when empty.
    body: the lane's own work, given the freshly opened session.
    """
    async with acting_as(principal_id, scopes) as session:
        return await body(session)


async def timed_lane(
    lane: Lane, principal_id: uuid.UUID, scopes: tuple[uuid.UUID, ...], ctx: RecallContext
) -> LaneResult:
    """Run one lane inside a span named for its own class, `gather_lanes`' per-lane timing seam.

    lane: the lane to run and time.
    principal_id: identity whose row level security visibility the lane's session acts under.
    scopes: group ids narrowing the lane's read to that combination's composed graph.
    ctx: the shared per-call inputs the lane reads its own slice off.
    """
    with span(type(lane).__name__):
        return await run_lane(principal_id, scopes, partial(lane.run, ctx=ctx))


async def gather_lanes(
    principal_id: uuid.UUID, scopes: tuple[uuid.UUID, ...], ctx: RecallContext
) -> list[LaneResult]:
    """Run every lane in `LANES` concurrently, each on its own session, and return their slices.

    principal_id: identity whose row level security visibility every lane's session acts under.
    scopes: group ids narrowing every lane's read to that combination's composed graph.
    ctx: the shared per-call inputs every lane reads its own slice off.
    """
    async with stage("recall_lanes"):
        return await asyncio.gather(
            *(timed_lane(lane, principal_id, scopes, ctx) for lane in LANES)
        )


@span
def fuse_lanes(parts: list[LaneResult]) -> LaneResult:
    """Concatenate every lane's own slice into one `LaneResult`, the whole fusion step.

    Each lane populates only its own field, so fusing is a plain per-field concatenation with no
    cross-lane deduplication to run, unlike `merge_facts`' statement-deduping merge of one lane's
    own seed, neighbor, and pagerank facts inside `Recall.assemble_context`.

    parts: one `LaneResult` per lane, in `LANES` order.
    """
    return LaneResult(
        hits=[hit for part in parts for hit in part.hits],
        facts=[fact for part in parts for fact in part.facts],
        session=[note for part in parts for note in part.session],
        communities=[note for part in parts for note in part.communities],
        raptor=[note for part in parts for note in part.raptor],
        profile=next((part.profile for part in parts if part.profile), None),
    )


def route(query: str) -> tuple[bool, bool, bool]:
    """Classify a query into its (thematic, ppr_on, raptor_on) lane gates.

    Reads `QueryRoute.plan` when query routing is on, narrowing the fixed lane mix to the route's
    own lanes. Otherwise falls back to the fixed settings toggles with only the thematic gate
    itself classified, the unrouted default.

    query: the natural-language query being recalled.
    """
    if not settings.query_routing:
        return QueryRoute.is_thematic(query), settings.ppr, settings.raptor
    plan = QueryRoute.plan(query)
    return plan.communities, plan.ppr, plan.raptor


async def build_context(query: str, k: int, as_of: datetime | None) -> RecallContext:
    """Embed the query and classify its route into one bound `RecallContext` every lane reads.

    query: natural-language query to recall context for.
    k: number of fused hits and of seed facts to surface.
    as_of: world-time the graph facts must be valid at, the live graph when null.
    """
    async with stage("embed_query"):
        [vector] = await Embedder().embed([query], mode="query")
    thematic, ppr_on, raptor_on = route(query)
    return RecallContext(
        query=query,
        vector=vector,
        k=k,
        as_of=as_of,
        thematic=thematic,
        ppr_on=ppr_on,
        raptor_on=raptor_on,
    )


async def recall(
    query: str,
    principal_id: uuid.UUID | None = None,
    k: int = 8,
    as_of: datetime | None = None,
    scopes: tuple[uuid.UUID, ...] = (),
) -> RecallResult:
    """Recall the fused chunk and graph context for a query, the agent's one retrieval entrypoint.

    Binds one `RecallContext`, fans it out across every lane in `LANES` concurrently, then fuses
    the lanes' own slices into one `RecallResult`. Building the context, running the lanes, and
    fusing them is the whole algorithm, read straight off this method.

    query: natural-language query to recall context for.
    principal_id: identity whose row level security visibility scopes the recall, the system
        principal when null.
    k: number of fused hits and of seed facts to surface.
    as_of: world-time the graph facts must be valid at, the live graph when null.
    scopes: group ids narrowing every lane's read to that combination's composed graph, the whole
        visible union when empty.
    """
    principal_id = principal_id or settings.system_principal_id
    ctx = await build_context(query, k, as_of)
    fused = fuse_lanes(await gather_lanes(principal_id, scopes, ctx))
    logger.info(
        "recall {query!r} bundled {hits} hits, {facts} facts, {comms} comms, {raptor} raptor",
        query=query,
        hits=len(fused.hits),
        facts=len(fused.facts),
        comms=len(fused.communities),
        raptor=len(fused.raptor),
    )
    return RecallResult(query=query, as_of=as_of, **fused.model_dump())
