from enum import StrEnum

from patos import FrozenModel

from ..config import settings

# markers that name a thematic query outright, the phrasings that ask for a global view rather than
# a specific fact, so recall reaches for the community summaries instead of only the local graph.
THEMATIC_MARKERS = (
    "overview",
    "summary",
    "summarize",
    "state of",
    "landscape",
    "themes",
    "overall",
    "in general",
    "big picture",
)

# markers that name a query asking how two named things connect, the phrasings a single dense match
# cannot answer because the answer lives on a path through the graph. Paired with two or more
# proper nouns they name the multi-hop route personalized pagerank widens reach for.
MULTIHOP_MARKERS = (
    "related to",
    "relationship",
    "connection",
    "connected",
    "link between",
    "linked to",
    "associated with",
    "how does",
    "how is",
    "lead to",
    "leads to",
    "led to",
    "path from",
    "between",
)


def named_entity_count(query: str) -> int:
    """Count the capitalized words past the first, a cheap stand-in for the named entities a query.

    Skips the leading word so a sentence-initial capital like How or When is not miscounted as a
    name, leaving only the mid-sentence capitals that actually mark proper nouns, the signal the
    multi-hop route reads to tell a two-entity relation from a one-entity lookup.

    query: the natural-language query whose named entities are counted.
    """
    return sum(1 for word in query.split()[1:] if word[:1].isupper())


class QueryRoute(StrEnum):
    """Which retrieval mix a recall reads, the Mnemis local-global-multihop split.

    LOCAL: a pointed lookup of a specific entity or fact, answered by the hybrid chunk and fact
        lanes alone, no global summaries and no graph walk.
    GLOBAL: a thematic question answered at the community and RAPTOR summary tier, the global view.
    MULTIHOP: a question about how named things connect, answered by the personalized-pagerank walk
        that reaches the facts a single dense match cannot.
    """

    LOCAL = "local"
    GLOBAL = "global"
    MULTIHOP = "multihop"

    @staticmethod
    def is_thematic(query: str) -> bool:
        """Decide whether a query asks for a global view rather than a specific fact.

        True when the query carries a thematic marker like overview or state of, or when it names
        at most one proper noun, the simple signal that it is broad enough for the community
        summaries to answer better than the local graph. A pointed query naming several proper
        nouns stays false.

        query: the natural-language query to classify.
        """
        lowered = query.casefold()
        if any(marker in lowered for marker in THEMATIC_MARKERS):
            return True
        proper = sum(1 for word in query.split() if word[:1].isupper())
        return proper <= 1

    @classmethod
    def classify(cls, query: str) -> QueryRoute:
        """Classify a query into its retrieval route with a cheap marker heuristic, no model call.

        A thematic marker names the GLOBAL route outright. A relational marker paired with two or
        more named entities names the MULTIHOP route, the case where the answer lives on a path
        between two named things. Everything else falls to the pointed LOCAL default.

        query: the natural-language query to route.
        """
        lowered = query.casefold()
        if any(marker in lowered for marker in THEMATIC_MARKERS):
            return cls.GLOBAL
        if named_entity_count(query) >= 2 and any(m in lowered for m in MULTIHOP_MARKERS):
            return cls.MULTIHOP
        return cls.LOCAL

    @classmethod
    def plan(cls, query: str) -> RoutePlan:
        """Plan which optional lanes a routed recall runs from the query's classified route.

        Narrows the fixed lane mix to the route's own lanes rather than widening it, so a lane a
        global setting has turned off stays off.

        query: the natural-language query being recalled.
        """
        route = cls.classify(query)
        match route:
            case cls.GLOBAL:
                return RoutePlan(route=route, ppr=False, communities=True, raptor=settings.raptor)
            case cls.MULTIHOP:
                return RoutePlan(route=route, ppr=settings.ppr, communities=False, raptor=False)
            case _:  # the pointed LOCAL default, the only remaining route classify returns
                return RoutePlan(route=route, ppr=False, communities=False, raptor=False)


class RoutePlan(FrozenModel):
    """The retrieval lanes a routed recall activates, the router's decision recall reads.

    route: the classified query route, kept for logging and the eval breakdown.
    ppr: whether the multi-hop personalized-pagerank lane runs, only on for the MULTIHOP route.
    communities: whether the global community summaries fold in, only ever on for the GLOBAL route.
    raptor: whether the RAPTOR summary tier folds in, on for the GLOBAL route when raptor is built.
    """

    route: QueryRoute
    ppr: bool
    communities: bool
    raptor: bool
