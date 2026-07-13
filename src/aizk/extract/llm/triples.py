from loguru import logger
from pydantic.main import BaseModel

from ...config import settings
from ...graph.consolidation import FactMatch
from .. import ontology
from ..dating import resolve_valid_from
from ..models import (
    BatchConsolidationVerdict,
    ConsolidationVerdict,
    ExtractedEntity,
    Extraction,
    TimedFact,
)
from .client import client_for
from .providers import provider_settings

_CONSOLIDATION_PROMPT = settings.consolidation_prompt


def extraction_system() -> str:
    """The ontology default strategy's system turn, the live ontology rules layered on the
    few-shot guidance from `settings.extract_system_prompt` that keeps entity names and
    facts well formed."""
    return f"{ontology.current().prompt}\n{settings.extract_system_prompt}"


async def structured[T: BaseModel](
    system: str,
    user: str,
    schema: type[T],
    *,
    temperature: float | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> T:
    """Run one schema-constrained chat turn and return the validated model instance."""
    # Resolve provider settings per call so concurrent requests share no mutable resolution.
    resolved = provider_settings()
    client = client_for(resolved.llm_url, resolved.llm_model, resolved.llm_api_key)
    completion = await client.chat.completions.parse(
        model=resolved.llm_model,
        response_format=schema,
        temperature=resolved.extract_temperature if temperature is None else temperature,
        timeout=resolved.extract_timeout if timeout is None else timeout,
        max_tokens=resolved.extract_max_tokens if max_tokens is None else max_tokens,
        # Local deployments may disable a model's reasoning preamble through extra_body.
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
    """Run the combined wire-schema extraction call under a given system prompt."""
    # The live ontology creates this response model dynamically.
    wire = await structured(system, text, ontology.current().llm_extraction)
    entities = [
        ExtractedEntity(name=entity.n, type=entity.t, suggested_type=entity.suggested_type)
        for entity in wire.e
    ]
    facts = [
        TimedFact(
            subject=fact.s,
            predicate=fact.p,
            object=fact.o,
            statement=fact.statement,
            quote=fact.quote,
            valid_from=resolve_valid_from(fact.date, fact.statement),
            kind=fact.k,
        )
        for fact in wire.f
    ]
    logger.info(
        "extracted {} entities and {} facts from {} chars", len(entities), len(facts), len(text)
    )
    return Extraction(entities=entities, facts=facts)


async def combined_extract(text: str) -> Extraction:
    """Extract entities, facts, and each fact's own date under the ontology default strategy."""
    return await extract_with_system(extraction_system(), text)


def consolidation_block(index: int, fact: TimedFact, existing: list[FactMatch]) -> str:
    """Render one candidate's new fact and its existing similar claims as a numbered prompt
    block."""
    catalog = (
        "\n".join(f"  id={claim.id} statement={claim.statement}" for claim in existing)
        or "  (none)"
    )
    return f"{index}. New fact: {fact.statement}\nExisting facts.\n{catalog}"


def resolve_verdict(
    index: int, existing: list[FactMatch], resolution: BatchConsolidationVerdict
) -> ConsolidationVerdict:
    """Resolve one candidate's verdict, dropping a supersedes id the batch call hallucinated."""
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
    candidates: list[tuple[TimedFact, list[FactMatch]]],
) -> list[ConsolidationVerdict]:
    """Decide ADD/UPDATE/NOOP for every borderline fact in one call."""
    if not candidates:
        return []
    user = "\n\n".join(
        consolidation_block(index, fact, existing)
        for index, (fact, existing) in enumerate(candidates)
    )
    resolution = await structured(_CONSOLIDATION_PROMPT, user, BatchConsolidationVerdict)
    results = [
        resolve_verdict(index, existing, resolution)
        for index, (_, existing) in enumerate(candidates)
    ]
    logger.info("batched consolidation resolved {} borderline facts in one call", len(candidates))
    return results
