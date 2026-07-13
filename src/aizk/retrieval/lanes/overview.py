from sqlalchemy import Integer, bindparam, func, select
from sqlalchemy.sql.selectable import Select

from ...common import sql
from ...extract import ontology
from ...store import EntityClaim, EntityContent
from ..models.lane import Lane, QueryContext


class OverviewLane(Lane):
    """The RAPTOR overview lane: root-level summaries ranked by embedding distance.

    The rendering reads EntityContent and EntityClaim jointly, so it stays with the
    query instead of either model.
    """

    kind: Lane.Kind = Lane.Kind.OVERVIEW

    def __call__(self, context: QueryContext) -> Select:
        """The deepest RAPTOR summaries under the query vector's distance order."""
        level = EntityClaim.attributes[int] >> "level"
        root_depth = (
            select(func.max(level))
            .join(EntityContent, EntityContent.id == EntityClaim.content_id)
            .where(EntityContent.type == ontology.RAPTOR_SUMMARY, level >= 1)
            .scalar_subquery()
        )
        raptor_distance = EntityContent.embedding @ context.vector
        return (
            self.row(
                evidence_id=EntityContent.id,
                ordering=raptor_distance,
                line=sql.concat(
                    t"- L{level} {EntityContent.name}: {EntityClaim.attributes >> 'summary'}"
                ),
                created_by=EntityClaim.created_by,
            )
            .select_from(EntityContent)
            .join(EntityClaim, EntityClaim.content_id == EntityContent.id)
            .where(
                EntityContent.type == ontology.RAPTOR_SUMMARY,
                level == root_depth,
                EntityContent.embedding.is_not(None),
            )
            .order_by(raptor_distance)
            .limit(bindparam("raptor_k", type_=Integer))
        )
