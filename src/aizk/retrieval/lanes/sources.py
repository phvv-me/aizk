from patos import sql
from sqlalchemy.sql.selectable import Select

from ...store import Chunk, Entity
from ..models.lane import Lane, QueryContext


class SourceLane(Lane):
    """Hybrid source retrieval as one lane.

    Dense and lexical chunk rankings fuse under the floor, promoted documents earn
    their bonus, hits cap per document, and each kept chunk renders as one scored
    source line.
    """

    kind: Lane.Kind = Lane.Kind.SOURCES

    def __call__(self, context: QueryContext) -> Select:
        """The capped hybrid chunk hits rendered as scored source lines."""
        hits = Chunk.hybrid(context)
        return self.row(
            evidence_id=hits.c.id,
            ordering=-hits.c.score,
            line=Chunk.source_line(hits),
            scopes=hits.c.scopes,
            source_chunk_id=hits.c.id,
            source_title=hits.c.document_title,
            source_uri=hits.c.source_uri,
            artifact_id=hits.c.artifact_id,
            artifact_content_id=hits.c.artifact_content_id,
            created_by=hits.c.created_by,
            direct=hits.c.direct,
        ).select_from(hits)


class EntityCatalogLane(Lane):
    """Live ontology entities grouped by type and exact scope set."""

    kind: Lane.Kind = Lane.Kind.SOURCES

    def __call__(self, context: QueryContext) -> Select:
        """Render query-relevant entity kinds and their current state facts."""
        catalog = Entity.catalog(context)
        return self.row(
            evidence_id=catalog.c.id,
            ordering=catalog.c.distance,
            line=sql.concat(t"Current {catalog.c.type} entities are {catalog.c.entries}."),
            scopes=catalog.c.scopes,
            source_title=sql.concat(t"{catalog.c.type} catalog"),
            created_by=catalog.c.created_by,
        ).select_from(catalog)
