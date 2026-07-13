import asyncio

from loguru import logger
from mainboard.profiling import span
from pydantic.types import PositiveInt

from ...config import settings
from ...serving import embed, named_entities
from ...store import FactClaim
from ...store.identity import User
from ..models import Candidate, Plan, QueryContext
from ..packing import pack
from ..rerank import rescore
from .program import build_recall_statement

_speaker_query_template = "{query}\nThe asking speaker is {label}."


async def query_entities(query: str) -> list[str]:
    """The lowered entity names a query mentions, the graph expansion's seeds.

    The statement compares `lower(name)` on the column side, expression-index friendly
    under Postgres's case-sensitive equality, so the gate lowers the names before binding.
    An off `graph_entity_seeding` skips the gate call entirely and seeds nothing, the
    eval harness's seeding ablation lever.
    """
    if not settings.graph_entity_seeding:
        return []
    return await named_entities(query)


@span("recall_context")
async def recall(
    query: str,
    user: User,
    k: PositiveInt = 8,
    token_budget: PositiveInt | None = None,
    plan: Plan | None = None,
) -> tuple[Candidate, ...]:
    """Retrieve the ranked, budget-fitted candidates from everything visible to `user`.

        embed | entities
              |
        recall statement, all lanes
              |
        cross-encoder rerank
              |
        Python budget walk
              |
        record fact access

    Every recall runs the maximal plan, all lanes on in facts-first order, with no
    query-time route classification. A misrouted query loses community and RAPTOR
    evidence the reranker cannot recover, overview-first packing buries fact evidence,
    and the zero-shot router measured 44% accuracy on the eval strata, so the plan is
    a constant rather than a classification.

    One statement cuts the candidates, the cross-encoder rescores the evidence lanes,
    and a plain Python walk packs the token budget. A final small transaction stamps the
    kept facts' access because retrieval strengthens memory, the statement ranks facts
    with a recency half-life over `last_accessed` blended with an `ln(1 + access_count)`
    frequency signal, so a fact must record each surfacing to stay warm.

    plan: a forced retrieval plan, the eval study's plan-forcing lever, while null
        runs the production maximal plan.
    """
    budget = token_budget if token_budget is not None else settings.context_token_budget
    resolved = plan if plan is not None else Plan.maximal()
    search_query = (
        _speaker_query_template.format(query=query, label=user.label) if user.label else query
    )
    embedded, named = await asyncio.gather(
        embed([search_query], mode="query"),
        query_entities(query),
    )
    [vector] = embedded
    context = QueryContext(dimensions=len(vector), fuzzy=settings.graph_mention_fuzzy)
    statement = build_recall_statement(context, resolved)
    rows = await user.exec[Candidate](
        statement,
        qvec=vector,
        qtext=search_query,
        qentities=named,
        k=k,
    )
    ranked = await rescore(rows, query)
    kept, used = pack(ranked, budget, settings.recall_chars_per_token)
    if accessed := [candidate.fact_id for candidate in kept if candidate.fact_id is not None]:
        async with user as session:
            await FactClaim.record_access(session, accessed)
    logger.info(
        "recall {query!r} selected {kept} candidates using {used}/{budget} tokens",
        query=query,
        kept=len(kept),
        used=used,
        budget=budget,
    )
    return kept
