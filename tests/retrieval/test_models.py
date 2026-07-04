from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel
from strategies import (
    community_notes,
    fact_hits,
    hits,
    raptor_notes,
    recall_results,
    session_notes,
    short_text,
    vector,
)

from aizk.retrieval import (
    Block,
    ChunkCandidate,
    CommunityNote,
    ContextPack,
    FactHit,
    Hit,
    LaneResult,
    RaptorNote,
    RecallContext,
    RecallResult,
    SessionNote,
)

blocks = st.builds(Block, lane=short_text, line=short_text)

chunk_candidates = st.builds(
    ChunkCandidate,
    id=st.uuids(),
    document_title=st.none() | short_text,
    source_uri=st.none() | short_text,
    text=short_text,
    promoted=st.booleans(),
)

context_packs = st.builds(
    ContextPack,
    query=short_text,
    blocks=st.lists(blocks, max_size=5),
    budget=st.integers(min_value=0, max_value=4000),
    used_tokens=st.integers(min_value=0, max_value=4000),
)

lane_results = st.builds(
    LaneResult,
    hits=st.lists(hits(), max_size=4),
    facts=st.lists(fact_hits(), max_size=4),
    session=st.lists(session_notes(), max_size=3),
    communities=st.lists(community_notes(), max_size=3),
    raptor=st.lists(raptor_notes(), max_size=3),
    profile=st.none() | short_text,
)

recall_contexts = st.builds(
    RecallContext,
    query=short_text,
    vector=vector(),
    k=st.integers(min_value=1, max_value=16),
    as_of=st.none(),
    thematic=st.booleans(),
    ppr_on=st.booleans(),
    raptor_on=st.booleans(),
)

# every retrieval result model, so one round-trip property guards the whole field contract at once.
result_models = st.one_of(
    hits(),
    fact_hits(),
    community_notes(),
    raptor_notes(),
    session_notes(),
    blocks,
    chunk_candidates,
    context_packs,
    lane_results,
    recall_contexts,
    recall_results(),
)


@given(model=result_models)
def test_result_models_round_trip_through_dump_and_validate(model: BaseModel) -> None:
    """Every result model reloads unchanged from its own dump, its field contract intact."""
    clone = type(model).model_validate(model.model_dump())
    assert clone == model


def test_lane_result_defaults_are_the_empty_slice() -> None:
    """A bare `LaneResult` is empty on every lane, the invariant a gated-off lane and fuse rely on.

    A lane whose own gate is off returns `LaneResult()` and `fuse_lanes` concatenates it as a
    no-op, so each field must default to its empty value rather than needing an explicit clear.
    """
    empty = LaneResult()
    assert empty.hits == []
    assert empty.facts == []
    assert empty.session == []
    assert empty.communities == []
    assert empty.raptor == []
    assert empty.profile is None


def test_recall_result_optional_lanes_default_empty() -> None:
    """A recall bundle built from only the required lanes leaves the optional lanes at empty."""
    bundle = RecallResult(query="q", hits=[], facts=[], communities=[], raptor=[], as_of=None)
    assert bundle.session == []
    assert bundle.profile is None


# the concrete single-item lanes, one of each result type, so the union above is anchored to real
# field names rather than only Hypothesis-built instances.
_ANCHORS: tuple[BaseModel, ...] = (
    Hit(document_title="T", source_uri="U", text="body", score=0.5),
    FactHit(statement="s", predicate="related_to", score=0.3, valid_from=None, valid_to=None),
    CommunityNote(label="c", summary="about", score=0.4),
    RaptorNote(label="theme", summary="broad", level=2, score=0.5),
    SessionNote(text="note", kind="note", score=0.2),
)


def test_result_anchors_round_trip() -> None:
    """The hand-built single-item lanes round-trip too, pinning the field names the union draws."""
    for model in _ANCHORS:
        assert type(model).model_validate(model.model_dump()) == model
