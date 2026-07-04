import asyncio
from collections import Counter
from datetime import UTC, datetime
from typing import NamedTuple, cast

import pytest
from doubles import RecordingReranker
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Result, Row
from sqlalchemy.dialects.postgresql import Range
from strategies import aware_datetimes, fact_hits, hits, predicates

import aizk.eval
from aizk.config import settings
from aizk.retrieval import FactHit, Hit, expand_query, merge_facts, merge_hits, rerank_hits
from aizk.retrieval.recall import fact_hits as render_fact_hits
from aizk.retrieval.recall import has_evidence_gap, sql_fact_hit, temporal_filter


@given(base=st.lists(fact_hits(), max_size=6), extra=st.lists(fact_hits(), max_size=6))
def test_merge_facts_keeps_base_first_and_dedups_extra_by_statement(
    base: list[FactHit], extra: list[FactHit]
) -> None:
    """The base survives verbatim and ahead, the tail adds only statements the base lacked."""
    merged = merge_facts(base, extra)
    assert merged[: len(base)] == base
    base_statements = {fact.statement for fact in base}
    tail = merged[len(base) :]
    assert all(fact.statement not in base_statements for fact in tail)
    assert len({fact.statement for fact in tail}) == len(tail)
    assert merge_facts(base, base) == list(base)


@given(
    base=st.lists(hits(), max_size=6),
    extra=st.lists(hits(), max_size=6),
    limit=st.integers(min_value=0, max_value=8),
)
def test_merge_hits_dedups_by_text_and_caps_at_limit(
    base: list[Hit], extra: list[Hit], limit: int
) -> None:
    """The cap bounds the merge, the base stays first within it, the tail adds only new texts."""
    merged = merge_hits(base, extra, limit)
    assert len(merged) <= limit
    keep = min(len(base), limit)
    assert merged[:keep] == base[:keep]
    base_texts = {hit.text for hit in base}
    assert all(hit.text not in base_texts for hit in merged[len(base) :])


@given(
    query=st.text(min_size=1, max_size=20),
    round_hits=st.lists(hits(), max_size=5),
    round_facts=st.lists(fact_hits(), max_size=5),
)
def test_expand_query_prefixes_the_query_and_is_unchanged_without_seeds(
    query: str, round_hits: list[Hit], round_facts: list[FactHit]
) -> None:
    """The widened query keeps the original ahead, falling back to it when nothing was recalled."""
    expanded = expand_query(query, round_hits, round_facts)
    if not round_hits and not round_facts:
        assert expanded == query
    else:
        assert expanded.startswith(query)
        seeds = [f.statement for f in round_facts[: settings.gap_seed_terms]]
        seeds += [h.text for h in round_hits[: settings.gap_seed_terms]]
        assert expanded == " ".join([query, *seeds])


class FactRow(NamedTuple):
    """A stand-in for one facts result row the fact_hits renderer reads positionally."""

    statement: str
    predicate: str
    valid: Range[datetime] | None
    distance: float


def valids() -> st.SearchStrategy[Range[datetime] | None]:
    """A null valid window or a well-ordered aware-time `Range`, the two shapes a row carries."""
    ordered = st.builds(lambda a, b: Range(min(a, b), max(a, b)), aware_datetimes, aware_datetimes)
    return st.none() | ordered


@given(
    rows=st.lists(
        st.builds(
            FactRow,
            statement=st.text(min_size=1, max_size=20),
            predicate=predicates,
            valid=valids(),
            distance=st.floats(min_value=0.0, max_value=2.0),
        ),
        max_size=8,
    ),
    margin=st.none() | st.floats(min_value=-1.0, max_value=1.0),
)
def test_fact_hits_scores_one_minus_distance_filters_and_unpacks_valid(
    rows: list[FactRow], margin: float | None
) -> None:
    """Kept facts score one-minus-distance, a margin drops the low ones, the window unpacks."""
    rendered = render_fact_hits(cast(Result, rows), margin=margin)
    kept = [row for row in rows if margin is None or 1.0 - row.distance >= margin]
    assert len(rendered) == len(kept)
    for hit, row in zip(rendered, kept, strict=True):
        assert hit.score == pytest.approx(1.0 - row.distance)
        assert hit.valid_from == (row.valid.lower if row.valid else None)
        assert hit.valid_to == (row.valid.upper if row.valid else None)
        if margin is not None:
            assert hit.score >= margin


@given(fact=fact_hits())
def test_sql_fact_hit_copies_a_row_by_matching_field_names(fact: FactHit) -> None:
    """The SQL row renderer maps a same-named row straight onto a FactHit, no field unpacking."""
    rendered = sql_fact_hit(cast(Row, fact))
    assert rendered == fact


def test_temporal_filter_lives_with_no_gate_and_replays_with_one() -> None:
    """A null as_of adds no predicate, a world-time lists visible_at and opts the live gate out."""
    gate, opts = temporal_filter(None)
    assert gate == [] and opts == {}
    gate, opts = temporal_filter(datetime(2020, 1, 1, tzinfo=UTC))
    assert gate and opts == {settings.skip_live_gate: True}


@given(
    query=st.text(min_size=1, max_size=16),
    pool=st.lists(hits(), max_size=8),
    k=st.integers(min_value=0, max_value=8),
)
def test_rerank_hits_sorts_by_reranker_score_and_truncates(
    query: str, pool: list[Hit], k: int, fake_reranker: RecordingReranker
) -> None:
    """The pool is rescored by the reranker's char overlap, sorted best first, cut to k."""
    reranked = asyncio.run(rerank_hits(query, pool, k))
    assert len(reranked) == min(len(pool), k)
    terms = set(query)
    snippet_chars = settings.rerank_snippet_chars
    expected = sorted(
        (float(len(terms & set(hit.text[:snippet_chars]))) for hit in pool), reverse=True
    )[:k]
    assert [hit.score for hit in reranked] == expected
    assert Counter(hit.text for hit in reranked) - Counter(hit.text for hit in pool) == Counter()


@given(round_hits=st.lists(hits(), max_size=6), min_hits=st.integers(min_value=0, max_value=8))
def test_has_evidence_gap_fires_below_the_hit_floor(
    round_hits: list[Hit], min_hits: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the score and judge checks off, a recall is thin when it carries too few hits."""
    monkeypatch.setattr(settings, "recall_gap_min_hits", min_hits)
    monkeypatch.setattr(settings, "recall_gap_min_score", 0.0)
    monkeypatch.setattr(settings, "recall_gap_judge", False)
    gap = asyncio.run(has_evidence_gap("q", round_hits, []))
    assert gap is (len(round_hits) < min_hits)


@given(round_hits=st.lists(hits(), max_size=6), min_score=st.floats(min_value=0.01, max_value=1.0))
def test_has_evidence_gap_fires_when_best_hit_misses_the_score_floor(
    round_hits: list[Hit], min_score: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the hit floor a recall is thin when its best hit cannot clear the score floor."""
    monkeypatch.setattr(settings, "recall_gap_min_hits", 0)
    monkeypatch.setattr(settings, "recall_gap_min_score", min_score)
    monkeypatch.setattr(settings, "recall_gap_judge", False)
    gap = asyncio.run(has_evidence_gap("q", round_hits, []))
    best = max((hit.score for hit in round_hits), default=0.0)
    assert gap is (best < min_score)


@pytest.mark.parametrize("answerable", [True, False])
def test_has_evidence_gap_delegates_to_the_judge_when_enabled(
    answerable: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the judge on, a gap is the judge calling the rendered context unanswerable."""

    async def fake_judge(question: str, context: str) -> bool:
        return answerable

    monkeypatch.setattr(aizk.eval, "judge_answerable", fake_judge)
    monkeypatch.setattr(settings, "recall_gap_min_hits", 0)
    monkeypatch.setattr(settings, "recall_gap_min_score", 0.0)
    monkeypatch.setattr(settings, "recall_gap_judge", True)
    round_hits = [Hit(document_title=None, source_uri=None, text="ctx", score=1.0)]
    gap = asyncio.run(has_evidence_gap("q", round_hits, []))
    assert gap is (not answerable)
