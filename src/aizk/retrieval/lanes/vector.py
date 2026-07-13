from sqlalchemy import Integer, bindparam
from sqlalchemy.sql.selectable import Select

from ...store import Community, Profile, SessionItem
from ..models.lane import Lane, QueryContext


class VectorLane(Lane):
    """Working memory, profile, and community summaries ranked purely by embedding
    distance.

    One class serves the three vector-only sections; the kind picks the table, the
    rendered line, the guards, and the section's own limit bind, always present with a
    route-independent limit since a zero limit simply yields an empty lane.
    """

    def __call__(self, context: QueryContext) -> Select:
        """This section's rows ranked by distance under its own configured limit."""
        match self.kind:
            case Lane.Kind.WORKING_MEMORY:
                return self.by_vector(
                    SessionItem.embedding,
                    SessionItem.line(),
                    SessionItem.id,
                    SessionItem.created_by,
                    bindparam("session_recall_k", type_=Integer),
                    SessionItem.promoted_at.is_(None),
                    vector=context.vector,
                    floor=context.floor,
                )
            case Lane.Kind.PROFILE:
                return self.by_vector(
                    Profile.embedding,
                    Profile.summary,
                    Profile.id,
                    Profile.created_by,
                    bindparam("profile_recall_k", type_=Integer),
                    vector=context.vector,
                    floor=context.floor,
                )
            case Lane.Kind.COMMUNITIES:
                return self.by_vector(
                    Community.embedding,
                    Community.line(),
                    Community.id,
                    Community.created_by,
                    bindparam("community_recall_k", type_=Integer),
                    vector=context.vector,
                    floor=context.floor,
                )
            case _:
                raise ValueError(f"{self.kind} is not a vector-only section")
