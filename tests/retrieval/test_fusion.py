from datetime import UTC, datetime
from typing import NamedTuple, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Result
from sqlalchemy.dialects.postgresql import Range
from strategies import predicates

from aizk.config import settings
from aizk.retrieval.recall import fact_hits, temporal_filter


class Row(NamedTuple):
    """A stand-in for one facts result row the fact_hits renderer reads positionally."""

    statement: str
    predicate: str
    valid: Range[datetime] | None
    distance: float


@given(
    rows=st.lists(
        st.builds(
            Row,
            statement=st.text(min_size=1, max_size=20),
            predicate=predicates,
            valid=st.none(),
            distance=st.floats(min_value=0.0, max_value=2.0),
        ),
        max_size=8,
    ),
    margin=st.none() | st.floats(min_value=-1.0, max_value=1.0),
)
def test_fact_hits_scores_one_minus_distance_and_filters_by_margin(
    rows: list[Row], margin: float | None
) -> None:
    """Every kept fact scores one-minus-distance, and a margin drops the facts beneath it."""
    rendered = fact_hits(cast(Result, rows), margin=margin)

    kept = [row for row in rows if margin is None or 1.0 - row.distance >= margin]
    assert len(rendered) == len(kept)
    for hit, row in zip(rendered, kept, strict=True):
        assert hit.score == pytest.approx(1.0 - row.distance)
        if margin is not None:
            assert hit.score >= margin


def test_temporal_filter_lives_with_no_gate_and_replays_with_one() -> None:
    """A null as_of adds no predicate, a world-time lists visible_at and opts the live gate out."""
    gate, opts = temporal_filter(None)
    assert gate == [] and opts == {}

    gate, opts = temporal_filter(datetime(2020, 1, 1, tzinfo=UTC))
    assert gate and opts == {settings.skip_live_gate: True}
