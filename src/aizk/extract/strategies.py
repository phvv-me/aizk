from ..config import settings
from .llm import combined_extract, extract_with_system
from .models import Extraction
from .ontology import ONTOLOGY_PROMPT

# the summary strategy's focus, layered on the ontology so its output still validates against
# the closed vocabulary, steering the model to the few highest-level entities and the claims that
# summarize the span rather than every mentioned detail, the coarse graph a broad recall reads.
SUMMARY_SYSTEM = f"{ONTOLOGY_PROMPT}\n{settings.extract_summary_prompt}"

# the preferences strategy's focus, layered on the ontology, steering the model to the durable
# choices and habits a person holds, the Decision, Pattern, and Gotcha facts a profile is built on.
PREFERENCES_SYSTEM = f"{ONTOLOGY_PROMPT}\n{settings.extract_preferences_prompt}"

# the named strategies whose system prompt is fixed at import time, the dispatch table
# extract_graph reads by settings.extract_strategy; "custom" is handled separately since its own
# prompt is read live off settings rather than frozen here, and the unmatched default falls
# through to combined_extract.
STRATEGY_SYSTEMS: dict[str, str] = {"summary": SUMMARY_SYSTEM, "preferences": PREFERENCES_SYSTEM}


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

    The ontology default and an empty custom prompt delegate to `combined_extract` so that path
    stays byte-for-byte deterministic for the property suite. Summary, preferences, and a filled
    custom prompt each layer their own focus on the shared ontology guidance, but still run
    through the same compact wire schema and dating cascade `extract_with_system` shares with it.

    text: the source span to extract from.
    """
    strategy = settings.extract_strategy
    if strategy == "custom" and settings.extract_custom_prompt:
        return await extract_with_system(custom_system(), text)
    system = STRATEGY_SYSTEMS.get(strategy)
    return await extract_with_system(system, text) if system else await combined_extract(text)
