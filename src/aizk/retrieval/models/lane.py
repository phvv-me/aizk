import abc
from collections.abc import Callable
from enum import StrEnum, auto
from typing import TYPE_CHECKING, cast

from patos import FrozenModel, sql
from pgvector.sqlalchemy import HALFVEC
from pydantic import UUID5, UUID7
from sqlalchemy import ColumnElement, Float, Integer, Text, bindparam, literal
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql.selectable import Select
from sqlalchemy.sql.type_api import TypeEngine
from sqlmodel import select

if TYPE_CHECKING:
    from patos.sql import Expr

type LaneRow = tuple[
    str,
    int,
    UUID5 | UUID7,
    float,
    str,
    list[UUID5],
    UUID7 | None,
    UUID7 | None,
    str | None,
    str | None,
    UUID7 | None,
    UUID7 | None,
    UUID5 | None,
    bool,
]
type LaneSelect = Select[LaneRow]
type OptionalUUID7Column = ColumnElement[UUID7] | ColumnElement[UUID7 | None] | None
type OptionalStrColumn = ColumnElement[str] | ColumnElement[str | None] | None

_provided_uuid7 = cast(
    "Callable[[OptionalUUID7Column], ColumnElement[UUID7 | None]]",
    sql.provided,
)
_provided_str = cast(
    "Callable[[OptionalStrColumn], ColumnElement[str | None]]",
    sql.provided,
)


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
    def vector(self) -> ColumnElement[list[float]]:
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
    def entities(self) -> ColumnElement[list[str]]:
        """The lowered entity names bind the graph expansion seeds from."""
        return bindparam("qentities", type_=cast("TypeEngine[list[str]]", ARRAY(Text)))


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
    def __call__(self, context: QueryContext) -> LaneSelect:
        """This lane's candidates for one query, in the shared row shape."""

    def row(
        self,
        evidence_id: ColumnElement[UUID5 | UUID7],
        ordering: ColumnElement[float],
        line: ColumnElement[str],
        scopes: ColumnElement[list[UUID5]],
        fact_id: ColumnElement[UUID7] | None = None,
        source_chunk_id: ColumnElement[UUID7 | None] | None = None,
        source_title: ColumnElement[str] | ColumnElement[str | None] | None = None,
        source_uri: ColumnElement[str | None] | None = None,
        artifact_id: ColumnElement[UUID7 | None] | None = None,
        artifact_content_id: ColumnElement[UUID7 | None] | None = None,
        created_by: ColumnElement[UUID5] | None = None,
        direct: ColumnElement[bool] | None = None,
    ) -> LaneSelect:
        """This lane's candidates in the shared column shape every lane unions into."""
        return cast(
            "LaneSelect",
            select(
                literal(self.kind.value).label("lane"),
                literal(self.priority).label("priority"),
                evidence_id.label("evidence_id"),
                ordering.label("ordering"),
            ).add_columns(
                line.label("line"),
                scopes.label("scopes"),
                _provided_uuid7(fact_id).label("fact_id"),
                _provided_uuid7(source_chunk_id).label("source_chunk_id"),
                _provided_str(source_title).label("source_title"),
                _provided_str(source_uri).label("source_uri"),
                _provided_uuid7(artifact_id).label("artifact_id"),
                _provided_uuid7(artifact_content_id).label("artifact_content_id"),
                sql.provided(created_by).label("created_by"),
                (literal(False) if direct is None else direct).label("direct"),
            ),
        )

    def by_vector(
        self,
        embedding: Expr[list[float] | None],
        line: ColumnElement[str],
        evidence_id: ColumnElement[UUID5 | UUID7],
        created_by: ColumnElement[UUID5],
        scopes: Expr[list[UUID5]],
        limit: ColumnElement[int],
        *guards: ColumnElement[bool],
        vector: ColumnElement[list[float]],
        floor: ColumnElement[float],
    ) -> LaneSelect:
        """This lane ranked by embedding distance, floored, ordered, and limited."""
        distance = embedding @ vector
        return (
            self.row(
                evidence_id=evidence_id,
                ordering=distance,
                line=line,
                scopes=scopes,
                created_by=created_by,
            )
            .where(embedding.is_not(None), *guards, distance < floor)
            .order_by(distance)
            .limit(limit)
        )
