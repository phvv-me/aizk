import asyncio

from loguru import logger
from mainboard.profiling import span
from pydantic.types import PositiveInt

from ...config import settings
from ...exceptions import OntologyNotReadyError
from ...ontology import Ontology
from ...serving.embed import embed
from ...serving.gate import named_entities
from ...store import Fact
from ...store.identity import User
from ..models import Candidate, Plan, QueryContext, RecallTrace
from ..packing import pack
from ..rerank import merit_order
from .program import build_recall_statement

_speaker_query_template = "{query}\nThe asking speaker is {label}."


async def query_entities(query: str, user: User) -> list[str]:
    """The lowered entity names a query mentions, the graph expansion's seeds.

    The statement compares `lower(name)` on the column side, expression-index friendly
    under Postgres's case-sensitive equality, so the gate lowers the names before binding.
    An off `graph_entity_seeding` skips the gate call entirely and seeds nothing, the
    diagnostic plan study's seeding ablation lever.
    """
    if not settings.graph_entity_seeding:
        return []
    try:
        Ontology.current()
    except OntologyNotReadyError:
        async with user as session:
            await Ontology.ensure(session)
    return await named_entities(query)


async def recall(
    query: str,
    user: User,
    k: PositiveInt = 8,
    token_budget: PositiveInt = settings.context_token_budget,
    plan: Plan | None = None,
) -> list[Candidate]:
    """Retrieve the ranked, budget-fitted candidates from everything visible to `user`.

        embed | entities
              |
        recall statement, all lanes
              |
        direct-source authority and cross-encoder rerank
              |
        Python budget walk
              |
        record fact access

    Every recall runs the maximal plan, all lanes on in facts-first order, with no
    query-time route classification. A misrouted query loses community and RAPTOR
    evidence the reranker cannot recover, overview-first packing buries fact evidence,
    and the zero-shot router measured 44% accuracy on the eval strata, so the plan is
    a constant rather than a classification.

    One statement cuts the candidates and marks sources whose complete title the question
    names. Those direct sources form the authoritative identity group while the cross-encoder
    orders evidence by merit inside that group and across all incidental evidence. A plain
    Python walk then packs the token budget. A final small transaction stamps the kept facts'
    access because retrieval strengthens memory. The statement ranks facts with a recency
    half-life over `last_accessed` blended with an `ln(1 + access_count)` frequency signal, so
    a fact must record each surfacing to stay warm.

    plan: a forced retrieval plan, the eval study's plan-forcing lever, while null
        runs the production maximal plan.
    """
    kept, _ = await _execute(query, user, k, token_budget, plan, record_access=True)
    return list(kept)


async def trace(
    query: str,
    user: User,
    k: PositiveInt = 8,
    token_budget: PositiveInt = settings.context_token_budget,
    plan: Plan | None = None,
) -> RecallTrace:
    """Explain one recall without changing fact access history."""
    _, diagnostic = await _execute(query, user, k, token_budget, plan, record_access=False)
    return diagnostic


@span("recall_context")
async def _execute(
    query: str,
    user: User,
    k: PositiveInt,
    token_budget: PositiveInt,
    plan: Plan | None,
    record_access: bool,
) -> tuple[tuple[Candidate, ...], RecallTrace]:
    """Run the statement, merit ordering, packing, and optional access write."""
    resolved = plan if plan is not None else Plan.maximal()
    search_query = (
        _speaker_query_template.format(query=query, label=user.label) if user.label else query
    )
    embedded, named = await asyncio.gather(
        embed([search_query], mode="query"),
        query_entities(query, user),
    )
    [vector] = embedded
    context = QueryContext(dimensions=len(vector), fuzzy=settings.graph_mention_fuzzy)
    rows = await user.exec[Candidate](
        build_recall_statement(context, resolved),
        qvec=vector,
        qtext=search_query,
        qentities=named,
        k=k,
    )
    ranking = await merit_order(rows, query)
    kept = tuple(pack(ranking.candidates, token_budget))
    if record_access and (
        accessed := [candidate.fact_id for candidate in kept if candidate.fact_id is not None]
    ):
        async with user as session:
            await Fact.Claim.record_access(session, accessed)
    logger.info(
        "recall {query!r} selected {kept} candidates within the {budget} token budget",
        query=query,
        kept=len(kept),
        budget=token_budget,
    )
    return kept, RecallTrace.build(
        query,
        token_budget,
        rows,
        ranking.candidates,
        kept,
        ranking.scores,
    )
