from patos import FrozenModel

from .community_note import CommunityNote
from .fact_hit import FactHit
from .hit import Hit
from .raptor_note import RaptorNote
from .session_note import SessionNote


class LaneResult(FrozenModel):
    """One lane's own slice of a recall bundle, before the lanes fuse into one `RecallResult`.

    Every lane populates only its own field and leaves the rest at their empty default, so fusing
    a batch of these is a plain per-field concatenation with no cross-lane knowledge needed.

    hits: fused, reranked chunk and fact evidence, the core lane's own contribution.
    facts: the matching latest facts, their one-hop neighbors, then the pagerank-reached facts,
        the core lane's own contribution.
    session: the still-working session items the query matched, the session lane's contribution.
    communities: global community summaries for a thematic query, the community lane's own.
    raptor: the recursive RAPTOR summaries, the RAPTOR lane's own contribution.
    profile: the top matched entity's rolled-up profile, the profile lane's own contribution.
    """

    hits: list[Hit] = []
    facts: list[FactHit] = []
    session: list[SessionNote] = []
    communities: list[CommunityNote] = []
    raptor: list[RaptorNote] = []
    profile: str | None = None
