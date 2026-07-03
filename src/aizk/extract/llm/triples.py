from datetime import UTC, datetime

from loguru import logger
from pydantic import BaseModel

from ...config import settings
from ...store import LiveFact
from ..models import (
    ConsolidationVerdict,
    ExtractedFact,
    Extraction,
    FactTimestamp,
    TimedFact,
    TimestampResolution,
)
from ..ontology import ONTOLOGY_PROMPT
from .client import client_for
from .providers import provider_settings

# the ontology default strategy's system turn, layered on the ontology rules, the few-shot
# guidance from settings.extract_system_prompt that keeps entity names and facts well formed.
EXTRACTION_SYSTEM = f"{ONTOLOGY_PROMPT}\n{settings.extract_system_prompt}"

# the timestamp pass's system turn, kept as a module constant so both `resolve_timestamps` and a
# test asserting the prompt shape read the same value settings.timestamp_resolution_prompt holds.
TIMESTAMP_PROMPT = settings.timestamp_resolution_prompt

# the consolidation pass's system turn, mirroring TIMESTAMP_PROMPT's role for the ADD/UPDATE/NOOP
# decision, sourced from settings.consolidation_prompt.
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
    client = client_for(resolved.llm_url, resolved.llm_model, resolved.llm_api_key)
    completion = await client.chat.completions.parse(
        model=resolved.llm_model,
        response_format=schema,
        temperature=resolved.extract_temperature if temperature is None else temperature,
        timeout=resolved.extract_timeout if timeout is None else timeout,
        max_tokens=resolved.extract_max_tokens if max_tokens is None else max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise ValueError(f"{resolved.llm_model} returned no parsed {schema.__name__}")
    return parsed


async def extract_triples(text: str) -> Extraction:
    """Extract an ontology-constrained graph slice from a text span.

    The endpoint's grammar-constrained decoding holds every entity type and predicate inside the
    ontology's Literal vocabularies, so an off-ontology value never reaches the graph. Entities and
    structural facts come back from one combined call, undated, since resolving valid-time competes
    with fact extraction and is left to `resolve_timestamps`.

    text: the source span to extract from.
    """
    extraction = await structured(EXTRACTION_SYSTEM, text, Extraction)
    logger.info(
        "extracted {} entities and {} facts from {} chars",
        len(extraction.entities),
        len(extraction.facts),
        len(text),
    )
    return extraction


async def resolve_timestamps(
    text: str,
    facts: list[ExtractedFact],
    *,
    reference_time: datetime | None = None,
) -> list[TimedFact]:
    """Date each extracted fact in a dedicated pass, run after structural extraction so date
    parsing never competes with fact extraction.

    Reads the source text and a reference time the relative dates resolve against, returning a
    valid_from and valid_to per fact aligned by position. A fact left undated, or a position the
    model omits, keeps a null window, so the bi-temporal gate treats it as always-holding.

    text: the source span the facts were extracted from, the evidence for their dates.
    facts: the structural facts to date, in the order the gate aligns timestamps against.
    reference_time: world-time the relative dates resolve against, now when None.
    """
    if not facts:
        return []
    reference = reference_time or datetime.now(UTC)
    catalog = "\n".join(f"{index}. {fact.statement}" for index, fact in enumerate(facts))
    user = f"Reference time.\n{reference.isoformat()}\n\nSource text.\n{text}\n\nFacts.\n{catalog}"
    resolution = await structured(TIMESTAMP_PROMPT, user, TimestampResolution)
    stamps = resolution.timestamps
    blank = FactTimestamp()
    dated = [
        TimedFact(
            subject=fact.subject,
            predicate=fact.predicate,
            object=fact.object_,
            statement=fact.statement,
            valid_from=(stamps[index] if index < len(stamps) else blank).valid_from,
            valid_to=(stamps[index] if index < len(stamps) else blank).valid_to,
        )
        for index, fact in enumerate(facts)
    ]
    logger.info("resolved valid-time for {} of {} facts", len(stamps), len(facts))
    return dated


async def decide_consolidation(
    new_fact: ExtractedFact,
    existing_facts: list[LiveFact],
) -> ConsolidationVerdict:
    """Decide whether a new fact is an ADD, an UPDATE, or a NOOP against existing claims.

    Asks the model to compare the candidate against the similar latest claims already in scope,
    naming the superseded claim id when it updates one. With no existing claims the answer is a
    trivial ADD, and a supersedes id outside the candidate set is dropped so a hallucinated id
    never retires a real claim.

    new_fact: the freshly extracted candidate fact.
    existing_facts: the similar live claims already stored in the same scope, `LiveFact.id` each
        one's own claim identity, the id a supersession names.
    """
    if not existing_facts:
        return ConsolidationVerdict(action="ADD")
    catalog = "\n".join(f"- id={fact.id} statement={fact.statement}" for fact in existing_facts)
    user = f"New fact.\n{new_fact.statement}\n\nExisting facts.\n{catalog}"
    verdict = await structured(CONSOLIDATION_PROMPT, user, ConsolidationVerdict)
    known = {fact.id for fact in existing_facts}
    supersedes = (
        verdict.supersedes if verdict.action == "UPDATE" and verdict.supersedes in known else None
    )
    logger.info("consolidation verdict {} supersedes {}", verdict.action, supersedes)
    return ConsolidationVerdict(action=verdict.action, supersedes=supersedes)
