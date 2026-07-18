from patos import sql
from sqlalchemy import Integer, bindparam, func
from sqlalchemy.sql.selectable import Select
from sqlmodel import select

from ...ontology import System
from ...store import Entity
from ..models.lane import Lane, QueryContext


class OverviewLane(Lane):
    """The RAPTOR overview lane: root-level summaries ranked by embedding distance.

    The rendering reads Entity.Content and Entity.Claim jointly, so it stays with the
    query instead of either model.
    """

    kind: Lane.Kind = Lane.Kind.OVERVIEW

    def __call__(self, context: QueryContext) -> Select:
        """The deepest RAPTOR summaries under the query vector's distance order."""
        level = Entity.Claim.attributes[int] >> "level"
        root_depth = (
            select(func.max(level))
            .join(Entity.Content, Entity.Content.id == Entity.Claim.content_id)
            .where(Entity.Content.type == System.Entity.RAPTOR_SUMMARY, level >= 1)
            .scalar_subquery()
        )
        raptor_distance = Entity.Content.embedding @ context.vector
        return (
            self.row(
                evidence_id=Entity.Content.id,
                ordering=raptor_distance,
                line=sql.concat(
                    t"- L{level} {Entity.Content.name}: {Entity.Claim.attributes >> 'summary'}"
                ),
                scopes=Entity.Claim.scopes,
                created_by=Entity.Claim.created_by,
            )
            .select_from(Entity.Content)
            .join(Entity.Claim, Entity.Claim.content_id == Entity.Content.id)
            .where(
                Entity.Content.type == System.Entity.RAPTOR_SUMMARY,
                level == root_depth,
                Entity.Content.embedding.is_not(None),
            )
            .order_by(raptor_distance)
            .limit(bindparam("raptor_k", type_=Integer))
        )
