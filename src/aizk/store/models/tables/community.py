import uuid

from sqlalchemy import Column, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field

from ...mixins import Embedded, Id, Scoped, TableBase, Timestamped


class Community(Id, Scoped, Timestamped, Embedded, TableBase, table=True):
    """A detected cluster of related entities with one LLM summary, the global-query lane.

    Community detection over the latest-fact entity graph groups densely connected entities, and
    each group is summarized once into a label and a paragraph the recall lane can match when a
    query is thematic rather than pointed. The summary embedding is what community_search ranks,
    and member_ids records which entities the cluster covers so a summary traces back to its facts.
    Rows are scoped and row-level-security forced exactly like entities and facts.

    id: stable identity, generated client-side on insert.
    owner_id: principal that owns the row, enforced by row level security.
    scopes: group set the row is shared with, empty when private to the owner.
    label: short human-readable name for the cluster.
    summary: paragraph describing what the cluster's entities and facts are about.
    embedding: halfvec dense vector of the summary, what community search ranks.
    member_ids: the entity content ids the cluster covers.
    created_at: build timestamp.
    """

    label: str = Field(sa_type=Text)
    summary: str = Field(sa_type=Text)
    member_ids: list[uuid.UUID] = Field(
        default_factory=list, sa_column=Column(ARRAY(Uuid), nullable=False)
    )
