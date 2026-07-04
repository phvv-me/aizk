from loguru import logger
from pydantic import BaseModel

from ...config import settings
from ...store import LiveFact
from ..dating import resolve_valid_from
from ..models import (
    BatchConsolidationVerdict,
    ConsolidationVerdict,
    ExtractedEntity,
    Extraction,
    LLMExtraction,
    TimedFact,
)
from ..ontology import ONTOLOGY_PROMPT
from .client import LLMClientPool
from .providers import provider_settings

# the ontology default strategy's system turn, layered on the ontology rules, the few-shot
# guidance from settings.extract_system_prompt that keeps entity names and facts well formed.
EXTRACTION_SYSTEM = f"{ONTOLOGY_PROMPT}\n{settings.extract_system_prompt}"

# the batched borderline-consolidation pass's system turn, mirroring EXTRACTION_SYSTEM's role,
# sourced from settings.consolidation_prompt.
CONSOLIDATION_PROMPT = settings.consolidation_prompt


async def structured[T: BaseModel](
    system: str,
    user: str,
    schema: type[T],
    *,
    temperature: float | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> T:
    """Run one schema-constrained chat turn and return the validated model instance.

    The single seam every extractor, judge, and summarizer flows through. Grammar-constrained
    decoding on the OpenAI-compatible endpoint means the returned message already validates
    against `schema` with no retry-until-valid layer on top. Takes no session or principal, so it
    is safe to call inside or outside a transaction.

    system: system prompt fixing the task and the response contract.
    user: user message carrying the content to reason over.
    schema: pydantic model the response must validate against, also the return type.
    temperature: sampling temperature, settings.extract_temperature when None.
    timeout: per-call wall-clock ceiling, settings.extract_timeout when None.
    max_tokens: hard output token cap, settings.extract_max_tokens when None.
    """
    # resolve the named provider preset once at the single LLM seam, so `AIZK_LLM_PROVIDER=ollama`
    # or a hosted provider name switches the endpoint while an explicit url or model still wins,
    # then hand the resolved endpoint to client_for as plain arguments so a concurrently running
    # `structured` call never observes another call's provider resolution.
    resolved = provider_settings()
    client = LLMClientPool().client_for(resolved.llm_url, resolved.llm_model, resolved.llm_api_key)
    completion = await client.chat.completions.parse(
        model=resolved.llm_model,
        response_format=schema,
        temperature=resolved.extract_temperature if temperature is None else temperature,
        timeout=resolved.extract_timeout if timeout is None else timeout,
        max_tokens=resolved.extract_max_tokens if max_tokens is None else max_tokens,
        # empty by default (see settings.llm_chat_template_kwargs), so a stock load sends no
        # extra_body and never risks a hosted OpenAI-shaped provider rejecting an unrecognized
        # field; the local vllm-llm deployment sets AIZK_LLM_CHAT_TEMPLATE_KWARGS to disable a
        # hybrid-thinking model's own <think> preamble, which would otherwise burn the combined
        # call's token budget on reasoning the closed ontology schema never asked for.
        extra_body={"chat_template_kwargs": resolved.llm_chat_template_kwargs}
        if resolved.llm_chat_template_kwargs
        else None,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise ValueError(f"{resolved.llm_model} returned no parsed {schema.__name__}")
    return parsed


async def extract_with_system(system: str, text: str) -> Extraction:
    """Run the combined wire-schema extraction call under a given system prompt.

    Every extraction strategy (`extract.strategies.extract_graph`'s ontology default, summary,
    preferences, and custom) shares this one call shape, only the system prompt's own focus
    differs. The compact wire schema (`LLMExtraction`) already carries an optional per-fact date
    alongside its entities and facts, so `extract.dating.resolve_valid_from` only ever needs the
    model's own field and the fact's own statement text, no second round trip. The wire shapes
    (`LLMEntity`/`LLMFact`, short keys to hold the call near the ~250-token budget) convert to the
    readable domain shapes (`ExtractedEntity`/`TimedFact`) immediately after parsing, so nothing
    downstream of this function ever reads the compact keys.

    system: system prompt fixing the strategy's own focus, layered on the shared ontology rules.
    text: the source span to extract from.
    """
    wire = await structured(system, text, LLMExtraction)
    entities = [ExtractedEntity(name=entity.n, type=entity.t) for entity in wire.e]
    facts = [
        TimedFact(
            subject=fact.s,
            predicate=fact.p,
            object=fact.o,
            statement=fact.statement,
            valid_from=resolve_valid_from(fact.date, fact.statement),
        )
        for fact in wire.f
    ]
    logger.info(
        "extracted {} entities and {} facts from {} chars", len(entities), len(facts), len(text)
    )
    return Extraction(entities=entities, facts=facts)


async def combined_extract(text: str) -> Extraction:
    """Extract entities, facts, and each fact's own date under the ontology default strategy.

    text: the source span to extract from.
    """
    return await extract_with_system(EXTRACTION_SYSTEM, text)


def consolidation_block(index: int, fact: TimedFact, existing: list[LiveFact]) -> str:
    """Render one candidate's new fact and its existing similar claims as a numbered prompt block.

    index: the candidate's position, the number the batch verdict is keyed back to.
    fact: the new fact awaiting a consolidation decision.
    existing: the candidate's own similar claims, the catalog the model chooses among.
    """
    catalog = (
        "\n".join(f"  id={claim.id} statement={claim.statement}" for claim in existing)
        or "  (none)"
    )
    return f"{index}. New fact: {fact.statement}\nExisting facts.\n{catalog}"


def resolve_verdict(
    index: int, existing: list[LiveFact], resolution: BatchConsolidationVerdict
) -> ConsolidationVerdict:
    """Resolve one candidate's verdict, dropping a supersedes id the batch call hallucinated.

    index: the candidate's position in the batch, aligned with the resolution's own verdicts.
    existing: the candidate's own similar claims, the only ids UPDATE may legally supersede.
    resolution: the batch call's raw verdicts, possibly shorter than the candidate list.
    """
    known = {claim.id for claim in existing}
    verdict = (
        resolution.verdicts[index]
        if index < len(resolution.verdicts)
        else ConsolidationVerdict(action="ADD")
    )
    supersedes = (
        verdict.supersedes if verdict.action == "UPDATE" and verdict.supersedes in known else None
    )
    return ConsolidationVerdict(action=verdict.action, supersedes=supersedes)


async def decide_consolidations_batch(
    candidates: list[tuple[TimedFact, list[LiveFact]]],
) -> list[ConsolidationVerdict]:
    """Decide ADD/UPDATE/NOOP for every borderline fact in one call.

    The non-LLM consolidation cascade (`graph.consolidation.decide_by_rule`) already resolves
    every candidate whose top similar claim falls outside the ambiguous cosine band. This is the
    batched tier it defers to for the rest, one call for a whole chunk's borderline facts together
    rather than one round trip per fact, the lever that keeps a chunk to at most two LLM calls
    total, the combined extraction call and this one.

    candidates: each borderline fact paired with its own similar existing claims, the same
        catalog a single-fact judge would have read, batched into one prompt instead.
    """
    if not candidates:
        return []
    user = "\n\n".join(
        consolidation_block(index, fact, existing)
        for index, (fact, existing) in enumerate(candidates)
    )
    resolution = await structured(CONSOLIDATION_PROMPT, user, BatchConsolidationVerdict)
    results = [
        resolve_verdict(index, existing, resolution)
        for index, (_, existing) in enumerate(candidates)
    ]
    logger.info("batched consolidation resolved {} borderline facts in one call", len(candidates))
    return results
