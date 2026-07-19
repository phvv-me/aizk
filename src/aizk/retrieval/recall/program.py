from functools import cache
from typing import cast

from pydantic import UUID5, UUID7
from sqlalchemy import union_all
from sqlmodel import select
from sqlmodel.sql.expression import Select

from ..models import Plan, QueryContext
from ..models.lane import LaneSelect

type RecallRow = tuple[
    str,
    UUID5 | UUID7,
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
type RecallSelect = Select[RecallRow]


@cache
def build_recall_statement(context: QueryContext, plan: Plan) -> RecallSelect:
    """Build one recall as a single RLS-filtered SQL program returning ranked candidates.

        dense seeds -- neighbors -- ppr hops       dense -- bm25
                        |                               |
                    fact lane    rrf source lane    memory    profiles    overviews
                        |               |               |         |           |
                        +------ union_all, ordered by the plan --------------+
                                                |
                                         candidate rows

    Each lane instance the plan yields renders its own Select in the shared row shape,
    and the union orders the candidates for the cut. Construction costs tens of
    milliseconds, so identical statements reuse one Select; the cache key is exactly
    what shapes the SQL tree, the frozen query context with the vector width and the
    trigram mention toggle, and the frozen plan with the lane order and graph toggles.
    Every tunable value binds at execution through `settings.for_statement`.

    The statement ends at the ordered candidate cut, and the caller reranks, packs,
    and records access in Python.
    """
    return ordered([lane(context) for lane in plan.lanes])


def ordered(lanes: list[LaneSelect]) -> RecallSelect:
    """Union every lane and order the candidates by plan priority then lane rank.

    The materialized cut keeps the planner from re-evaluating the whole union per output
    row, so `ordered_context` must stay MATERIALIZED. Priority and ordering stay internal
    to the sort while the projected columns are exactly the Candidate payload.

    `add_columns` erases the sqlmodel `Select` subtype back to a plain SQLAlchemy `Select`
    statically, yet the runtime object stays a sqlmodel `Select`, so the cast restores the
    type `AsyncSession.exec` requires without changing what runs.
    """
    candidates = union_all(*lanes).cte("ordered_context").prefix_with("MATERIALIZED")
    return cast(
        "RecallSelect",
        select(
            candidates.c.lane,
            candidates.c.evidence_id,
            candidates.c.line,
            candidates.c.scopes,
        )
        .add_columns(
            candidates.c.fact_id,
            candidates.c.source_chunk_id,
            candidates.c.source_title,
            candidates.c.source_uri,
            candidates.c.artifact_id,
            candidates.c.artifact_content_id,
            candidates.c.created_by,
            candidates.c.direct,
        )
        .order_by(candidates.c.priority, candidates.c.ordering, candidates.c.evidence_id),
    )
