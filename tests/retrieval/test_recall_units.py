import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st
from strategies import fact_hits, hits

import aizk.eval
from aizk.config import settings
from aizk.retrieval import FactHit, Hit, expand_query, merge_facts, merge_hits
from aizk.retrieval.recall import has_evidence_gap


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


@given(
    round_hits=st.lists(hits(), max_size=6),
    min_hits=st.integers(min_value=0, max_value=8),
)
def test_has_evidence_gap_fires_when_below_the_hit_floor(
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
    round_hits = [Hit(document_title=None, source_uri=None, text="ctx", score=1.0)]

    monkeypatch.setattr(settings, "recall_gap_min_hits", 0)
    monkeypatch.setattr(settings, "recall_gap_min_score", 0.0)
    monkeypatch.setattr(settings, "recall_gap_judge", True)
    gap = asyncio.run(has_evidence_gap("q", round_hits, []))
    assert gap is (not answerable)
