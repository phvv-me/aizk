import asyncio
from collections import Counter
from collections.abc import Awaitable, Hashable
from time import perf_counter
from typing import cast

from loguru import logger
from pydantic import UUID7
from pydantic.types import PositiveInt

from ...common.observability import span
from ...config import settings
from ...config.settings import StatementValue
from ...serving.embed import EmbedClient
from ...serving.gate import GateClient
from ...store import Fact
from ...store.identity import User
from ...types import Scopes
from ..models import Candidate, FindEvidence, FindTiming, FindTrace, Plan, QueryContext
from ..packing import deduplicate, pack
from ..rerank import MeritOrder, merit_order
from .program import build_find_statement

_speaker_query_template = "{query}\nThe asking speaker is {label}."


async def _timed[Result](name: str, operation: Awaitable[Result]) -> tuple[Result, float]:
    """Await one find phase under a matching OpenTelemetry span and wall clock."""
    started_at = perf_counter()
    with span(name):
        result = await operation
    return result, (perf_counter() - started_at) * 1000


async def _record_accesses(user: User, accessed: list[UUID7]) -> None:
    """Record the surfaced facts in one caller-bound transaction."""
    async with user as session:
        await Fact.Claim.record_access(session, accessed)


async def query_entities(query: str) -> list[str]:
    """The lowered entity names a query mentions, the graph expansion's seeds.

    The statement compares `lower(name)` on the column side, expression-index friendly
    under Postgres's case-sensitive equality, so the gate lowers the names before binding.
    An off `graph_entity_seeding` skips the gate call entirely and seeds nothing, the
    diagnostic plan study's seeding ablation lever.
    """
    if not settings.find_graph_expansion_enabled or not settings.graph_entity_seeding:
        return []
    return await GateClient.from_settings(settings).named_entities(query)


async def find(
    query: str,
    user: User,
    k: PositiveInt = 8,
    token_budget: PositiveInt = settings.context_token_budget,
    plan: Plan | None = None,
) -> list[Candidate]:
    """Retrieve the ranked, budget-fitted candidates from everything visible to `user`.

        embed | entities
              |
        find statement, all lanes
              |
        direct-source authority and cross-encoder rerank
              |
        drop repeated spans, then the Python budget walk
              |
        record fact access

    Every find runs the maximal plan, all lanes on in facts-first order, with no
    query-time route classification. A misrouted query loses community and RAPTOR
    evidence the reranker cannot recover, overview-first packing buries fact evidence,
    and the zero-shot router measured 44% accuracy on the eval strata, so the plan is
    a constant rather than a classification.

    One statement cuts the candidates and marks sources whose complete title the question
    names. Those direct sources form the authoritative identity group while the cross-encoder
    orders evidence by merit inside that group and across all incidental evidence. Evidence
    repeating a span a better-ranked item already carries then falls away, and a plain Python
    walk packs what remains into the token budget. A final small transaction stamps the facts'
    access because retrieval strengthens memory. The statement ranks facts with a recency
    half-life over `last_accessed` blended with an `ln(1 + access_count)` frequency signal, so
    a fact must record each surfacing to stay warm.

    plan: a forced retrieval plan, the eval study's plan-forcing lever, while null
        runs the production maximal plan.
    """
    return list((await evidence(query, user, k, token_budget, plan)).candidates)


async def evidence(
    query: str,
    user: User,
    k: PositiveInt = 8,
    token_budget: PositiveInt = settings.context_token_budget,
    plan: Plan | None = None,
) -> FindEvidence:
    """The same retrieval `find` runs, keeping the scores and mentions it computed.

    `find` reads both to decide whether the public web could add anything, so the decision
    costs one dictionary lookup rather than a second pass over the same evidence.
    """
    kept, ranking, named, _ = await _execute(
        query, user, k, token_budget, plan, record_access=True
    )
    return FindEvidence(candidates=kept, scores=ranking.scores, mentions=tuple(named))


async def trace(
    query: str,
    user: User,
    k: PositiveInt = 8,
    token_budget: PositiveInt = settings.context_token_budget,
    plan: Plan | None = None,
) -> FindTrace:
    """Explain one find without changing fact access history."""
    _, _, _, diagnostic = await _execute(query, user, k, token_budget, plan, record_access=False)
    return diagnostic


async def documents(
    query: str, user: User, limit: PositiveInt, scopes: Scopes | None = None
) -> list[UUID7]:
    """The distinct source documents one question names, in merit order, capped at `limit`.

    Selection runs the same lanes and the same cross-encoder merit ordering `find` runs and
    then walks the whole ranking by document instead of by token budget, so a caller choosing
    what to share sees the documents behind the evidence a find would have shown it. The
    walk reads the ranking rather than the packed prefix because a prompt budget that hides
    the tenth document must not silently shrink a share. Selection never records fact access,
    since choosing what to move is not keeping.

    scopes: keep only evidence standing in exactly this scope set. The source lane applies
        the same predicate inside its own rankings, so the per-lane cut is already spent on
        eligible documents alone. That ordering is what makes a repeated share stable: the
        copies an earlier share left in an organization outrank their private originals
        under the promoted bonus, and a lane that cut first would let them crowd the
        originals out of the selection before this walk ever saw them.
    """
    _, ranking, _, _ = await _execute(
        query,
        user,
        k=limit * settings.find_per_document,
        token_budget=settings.context_token_budget,
        plan=None,
        record_access=False,
        scopes=scopes,
    )
    named = dict.fromkeys(
        candidate.document_id
        for candidate in ranking.candidates
        if candidate.document_id is not None and (scopes is None or candidate.scopes == scopes)
    )
    return list(named)[:limit]


async def _execute(
    query: str,
    user: User,
    k: PositiveInt,
    token_budget: PositiveInt,
    plan: Plan | None,
    record_access: bool,
    scopes: Scopes | None = None,
) -> tuple[tuple[Candidate, ...], MeritOrder, list[str], FindTrace]:
    """Run the statement, merit ordering, packing, and optional access write.

    The packed prefix, the complete merit ordering with its scores, the query mentions, and
    the diagnostic trace all leave here because each caller needs a different one of the
    four. An exact scope set narrows the source lane inside the statement, so the
    restriction shapes the SQL and rides in the statement cache key rather than being
    applied to whatever the lane happened to return.
    """
    started_at = perf_counter()
    resolved = plan if plan is not None else Plan.maximal()
    search_query = (
        _speaker_query_template.format(query=query, label=user.label) if user.label else query
    )
    embedded_result, entity_result = await asyncio.gather(
        _timed(
            "find_embedding",
            EmbedClient.from_settings(settings).embed([search_query], mode="query"),
        ),
        _timed("find_entity_detection", query_entities(query)),
    )
    embedded, embedding_ms = embedded_result
    named, entity_detection_ms = entity_result
    [vector] = embedded
    context = QueryContext(
        dimensions=len(vector), fuzzy=settings.graph_mention_fuzzy, owned=scopes is not None
    )
    restriction: dict[str, StatementValue] = (
        {} if scopes is None else {"qscopes": [str(scope) for scope in sorted(scopes)]}
    )
    rows, database_ms = await _timed(
        "find_database",
        user.exec[Candidate](
            build_find_statement(cast("Hashable", context), cast("Hashable", resolved)),
            qvec=vector,
            qtext=search_query,
            qentities=named,
            k=k,
            **restriction,
        ),
    )
    ranking, rerank_ms = await _timed("find_rerank", merit_order(rows, query))
    packing_started_at = perf_counter()
    with span("find_packing"):
        kept = tuple(pack(deduplicate(ranking.candidates), token_budget))
    packing_ms = (perf_counter() - packing_started_at) * 1000
    access_recording_ms = 0.0
    if (
        settings.find_access_recording_enabled
        and record_access
        and (
            accessed := [candidate.fact_id for candidate in kept if candidate.fact_id is not None]
        )
    ):
        _, access_recording_ms = await _timed(
            "find_access_recording", _record_accesses(user, accessed)
        )
    timing = FindTiming(
        total_ms=(perf_counter() - started_at) * 1000,
        embedding_ms=embedding_ms,
        entity_detection_ms=entity_detection_ms,
        database_ms=database_ms,
        rerank_ms=rerank_ms,
        packing_ms=packing_ms,
        access_recording_ms=access_recording_ms,
        statement_rows=len(rows),
        selected_rows=len(kept),
        statement_lanes=dict(Counter(candidate.lane.value for candidate in rows)),
        selected_lanes=dict(Counter(candidate.lane.value for candidate in kept)),
    )
    if settings.profiling:
        logger.info("find profile {}", timing.model_dump_json())
    logger.info(
        "find selected {kept} candidates within the {budget} token budget",
        kept=len(kept),
        budget=token_budget,
    )
    return (
        kept,
        ranking,
        named,
        FindTrace.build(
            query,
            token_budget,
            rows,
            ranking.candidates,
            kept,
            ranking.scores,
            timing,
        ),
    )
