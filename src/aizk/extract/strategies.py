from loguru import logger

from ..config import settings
from .llm import structured
from .llm.triples import extract_triples
from .models import Extraction
from .ontology import ONTOLOGY_PROMPT

# the summary strategy's focus, layered on the ontology so its output still validates against
# the closed vocabulary, steering the model to the few highest-level entities and the claims that
# summarize the span rather than every mentioned detail, the coarse graph a broad recall reads.
SUMMARY_SYSTEM = f"{ONTOLOGY_PROMPT}\n{settings.extract_summary_prompt}"

# the preferences strategy's focus, layered on the ontology, steering the model to the durable
# choices and habits a person holds, the Decision, Pattern, and Gotcha facts a profile is built on.
PREFERENCES_SYSTEM = f"{ONTOLOGY_PROMPT}\n{settings.extract_preferences_prompt}"


def custom_system() -> str:
    """The custom strategy's system prompt, the bare ontology when no focus is configured.

    A deployment sets AIZK_EXTRACT_CUSTOM_PROMPT to steer extraction toward its own focus, layered
    on the ontology so the output still validates against the closed vocabulary. An empty prompt
    leaves the bare ontology guidance in place.
    """
    if not settings.extract_custom_prompt:
        return ONTOLOGY_PROMPT
    return f"{ONTOLOGY_PROMPT}\n{settings.extract_custom_prompt}"


async def extract_graph(text: str) -> Extraction:
    """Extract a graph slice from a span under the strategy settings.extract_strategy names.

    The ontology default and an empty custom prompt delegate to `extract_triples` so that path
    stays byte-for-byte deterministic for the property suite. Summary, preferences, and a filled
    custom prompt each layer their own focus on the shared ontology guidance before running the
    one LLM seam.

    text: the source span to extract from.
    """
    strategy = settings.extract_strategy
    if strategy == "summary":
        system = SUMMARY_SYSTEM
    elif strategy == "preferences":
        system = PREFERENCES_SYSTEM
    elif strategy == "custom" and settings.extract_custom_prompt:
        system = custom_system()
    else:
        return await extract_triples(text)

    extraction = await structured(system, text, Extraction)
    logger.info(
        "{} extracted {} entities and {} facts from {} chars",
        strategy,
        len(extraction.entities),
        len(extraction.facts),
        len(text),
    )
    return extraction
