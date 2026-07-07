import re

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..extract import ontology
from ..store.models.tables.ontology import EntityKind
from .consolidation import cosine_similarity

# words a suggestion's own free text folds into a PascalCase type name from, three at most so a
# whole sentence never becomes the name, "a financial goal for the house down payment" mints
# FinancialGoalFor rather than the entire clause.
NAME_WORDS = re.compile(r"[A-Za-z0-9]+")
MAX_NAME_WORDS = 3


def derive_type_name(suggested: str) -> str:
    """A PascalCase entity kind name from the extractor's own free-text suggestion.

    suggested: the extractor's own guess at what `Concept` was really standing in for.
    """
    words = NAME_WORDS.findall(suggested)[:MAX_NAME_WORDS]
    return "".join(word.capitalize() for word in words) or ontology.CONCEPT


def best_matching_kind(vector: list[float]) -> tuple[str, float] | None:
    """The entity kind whose description sits closest to `vector`, or null when the live snapshot
    carries no description to compare against.

    vector: the suggestion's own embedded text, the auto-create cascade's moving side.
    """
    scored = [
        (name, cosine_similarity(vector, candidate))
        for name, candidate in ontology.current().entity_description_vectors.items()
    ]
    return max(scored, key=lambda pair: pair[1]) if scored else None


async def resolve_suggested_type(session: AsyncSession, suggested: str) -> str:
    """Fold `suggested` into an existing entity kind or mint a fresh one, returning the name an
    entity typed `Concept` should actually carry.

    Cheapest signal first, the same rules-first posture `graph.consolidation`'s ADD/UPDATE/NOOP
    cascade already earns its keep with for facts. Embedding similarity against every known
    description decides the fold, the canonicalization the schema-induction papers describe done
    preventively at create time rather than as a later merge pass, and only a suggestion past no
    match or below `settings.ontology_growth_threshold` mints. No LLM call backs the ambiguous
    middle the way the fact cascade's borderline band gets one, tuning this single threshold
    against real extraction is a measured, deployment-specific follow-up rather than a call this
    makes up front.

    session: open session the row is written through, `entity_kind` carries no row level
        security so any session works.
    suggested: the extractor's own free-text guess at what `Concept` was really standing in for.
    """
    from ..serving import Embedder

    [vector] = await Embedder().embed([suggested], mode="document")
    match = best_matching_kind(vector)
    name = (
        match[0]
        if match is not None and match[1] >= settings.ontology_growth_threshold
        else derive_type_name(suggested)
    )
    await EntityKind.mint(session, name=name, description=suggested, domain="auto")
    await ontology.refresh(session)
    return name
