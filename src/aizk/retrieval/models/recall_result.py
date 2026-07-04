from datetime import datetime

from patos import FrozenModel

from .community_note import CommunityNote
from .fact_hit import FactHit
from .hit import Hit
from .raptor_note import RaptorNote
from .session_note import SessionNote


class RecallResult(FrozenModel):
    """The single fused context a recall returns, the agent's one retrieval surface.

    The agent calls recall and reads this directly as structured data, never deciding the
    chunk-versus-graph mix itself and never parsing a rendered string back apart.

    query: the natural-language query this context answers.
    hits: fused, reranked chunk and fact evidence, best first.
    facts: the matching latest facts, their one-hop neighbors, then the pagerank-reached facts.
    communities: global community summaries for a thematic query, empty for a pointed one.
    raptor: the recursive RAPTOR summaries, the root level for a thematic query and the leaf
        summary level for a pointed one, empty until a tree is built or when the lane is off.
    session: the still-working session items the query matched, the fast front of memory whose
        knowledge has not yet reached the graph, empty when the working lane is off.
    profile: the static-plus-dynamic profile of the top matched entity, null unless profiles on.
    as_of: world-time the graph was read at, the live graph when null.
    """

    query: str
    hits: list[Hit]
    facts: list[FactHit]
    communities: list[CommunityNote]
    raptor: list[RaptorNote]
    session: list[SessionNote] = []
    profile: str | None = None
    as_of: datetime | None
