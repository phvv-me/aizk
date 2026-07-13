import abc
import uuid
from enum import StrEnum, auto
from typing import TYPE_CHECKING

from patos import FrozenModel
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import ColumnElement, Float, Integer, Text, bindparam, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql.selectable import Select

from ...common import sql

if TYPE_CHECKING:
    from ...common.sql import Expr


class QueryContext(FrozenModel):
    """One query's statement-shaping knobs and the named binds every lane draws from.

    Only the two fields shape the SQL tree, which makes the frozen context half of the
    statement cache key beside the plan. The bind properties mint fresh `bindparam`
    objects on each read; SQLAlchemy unifies them by name at compile time, so every
    lane reading the same property lands on one execution parameter.
    """

    dimensions: int
    fuzzy: bool

    @property
    def vector(self) -> ColumnElement:
        """The query embedding bind, typed to this context's vector width."""
        return bindparam("qvec", type_=HALFVEC(self.dimensions))

    @property
    def k(self) -> ColumnElement[int]:
        """The caller-provided per-lane candidate budget bind."""
        return bindparam("k", type_=Integer)

    @property
    def floor(self) -> ColumnElement[float]:
        """The cosine-distance ceiling bind every dense ranking guards under."""
        return bindparam("recall_max_distance", type_=Float)

    @property
    def fusion_depth(self) -> ColumnElement[int]:
        """The per-ranking cut bind each fused ranking reads before merging."""
        return bindparam("fusion_depth", type_=Integer)

    @property
    def entities(self) -> ColumnElement:
        """The lowered entity names bind the graph expansion seeds from."""
        return bindparam("qentities", type_=ARRAY(Text))


class Lane(FrozenModel, abc.ABC):
    """One evidence lane of the recall union.

    It carries the context section the lane fills and its priority under the plan,
    and every subclass renders its candidates into the one column shape all lanes
    union into by calling the lane with the query context. Priority and ordering stay
    internal to the statement, which sorts by them and projects only the candidate
    payload.
    """

    class Kind(StrEnum):
        """The ordered evidence sections recall returns."""

        PROFILE = auto()
        OVERVIEW = auto()
        COMMUNITIES = auto()
        FACTS = auto()
        WORKING_MEMORY = auto()
        SOURCES = auto()

    kind: Kind
    priority: int

    @abc.abstractmethod
    def __call__(self, context: QueryContext) -> Select:
        """This lane's candidates for one query, in the shared row shape."""

    def row(
        self,
        evidence_id: ColumnElement[uuid.UUID],
        ordering: ColumnElement[float],
        line: ColumnElement[str],
        fact_id: ColumnElement[uuid.UUID] | None = None,
        source_chunk_id: ColumnElement[uuid.UUID | None] | None = None,
        source_title: ColumnElement[str | None] | None = None,
        source_uri: ColumnElement[str | None] | None = None,
        created_by: ColumnElement[uuid.UUID] | None = None,
    ) -> Select:
        """This lane's candidates in the shared column shape every lane unions into."""
        return select(
            literal(self.kind.value).label("lane"),
            literal(self.priority).label("priority"),
            evidence_id.label("evidence_id"),
            ordering.label("ordering"),
            line.label("line"),
            sql.provided(fact_id).label("fact_id"),
            sql.provided(source_chunk_id).label("source_chunk_id"),
            sql.provided(source_title).label("source_title"),
            sql.provided(source_uri).label("source_uri"),
            sql.provided(created_by).label("created_by"),
        )

    def by_vector(
        self,
        embedding: Expr[list[float] | None],
        line: ColumnElement[str],
        evidence_id: ColumnElement[uuid.UUID],
        created_by: ColumnElement[uuid.UUID],
        limit: ColumnElement[int],
        *guards: ColumnElement[bool],
        vector: ColumnElement,
        floor: ColumnElement[float],
    ) -> Select:
        """This lane ranked by embedding distance, floored, ordered, and limited."""
        distance = embedding @ vector
        return (
            self.row(evidence_id=evidence_id, ordering=distance, line=line, created_by=created_by)
            .where(embedding.is_not(None), *guards, distance < floor)
            .order_by(distance)
            .limit(limit)
        )
