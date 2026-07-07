from ..config import settings
from . import ontology
from .llm import combined_extract, extract_with_system
from .models import Extraction


def summary_system() -> str:
    """The summary strategy's focus, layered on the live ontology so its output still validates
    against the current catalog, steering the model to the few highest-level entities and the
    claims that summarize the span rather than every mentioned detail, the coarse graph a broad
    recall reads.
    """
    return f"{ontology.current().prompt}\n{settings.extract_summary_prompt}"


def preferences_system() -> str:
    """The preferences strategy's focus, layered on the live ontology, steering the model to the
    durable choices and habits a person holds, the Decision, Pattern, and Gotcha facts a profile
    is built on.
    """
    return f"{ontology.current().prompt}\n{settings.extract_preferences_prompt}"


def custom_system() -> str:
    """The custom strategy's system prompt, the bare ontology when no focus is configured.

    A deployment sets AIZK_EXTRACT_CUSTOM_PROMPT to steer extraction toward its own focus, layered
    on the live ontology so the output still validates against the current catalog. An empty
    prompt leaves the bare ontology guidance in place.
    """
    if not settings.extract_custom_prompt:
        return ontology.current().prompt
    return f"{ontology.current().prompt}\n{settings.extract_custom_prompt}"


# the named strategies whose system prompt layers a fixed focus over the live ontology, the
# dispatch table extract_graph reads by settings.extract_strategy; each entry is the prompt
# builder itself, not its already-rendered text, since the live catalog can grow between calls.
# "custom" is handled separately since its own prompt also depends on settings.extract_custom_
# prompt, and the unmatched default falls through to combined_extract.
STRATEGY_SYSTEMS = {"summary": summary_system, "preferences": preferences_system}


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
    build_system = STRATEGY_SYSTEMS.get(strategy)
    return (
        await extract_with_system(build_system(), text)
        if build_system
        else await combined_extract(text)
    )
