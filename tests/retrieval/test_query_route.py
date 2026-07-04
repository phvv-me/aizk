import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.config import settings
from aizk.retrieval.query_route import (
    MULTIHOP_MARKERS,
    THEMATIC_MARKERS,
    QueryRoute,
    RoutePlan,
    named_entity_count,
)

words = st.text(alphabet=st.characters(categories=["Ll", "Lu"]), min_size=1, max_size=8)


@given(query=st.lists(words, min_size=1, max_size=8).map(" ".join))
def test_named_entity_count_skips_the_leading_word(query: str) -> None:
    """The count is the capitalized words past the first, skipping the sentence-initial capital."""
    expected = sum(1 for word in query.split()[1:] if word[:1].isupper())
    assert named_entity_count(query) == expected


@pytest.mark.parametrize("marker", THEMATIC_MARKERS)
def test_thematic_marker_routes_global(marker: str) -> None:
    """Any thematic marker names the GLOBAL route outright, whatever the proper-noun count."""
    assert QueryRoute.classify(f"give me an {marker} of Alice and Bob and Carol") == (
        QueryRoute.GLOBAL
    )


@pytest.mark.parametrize("marker", MULTIHOP_MARKERS)
def test_relational_marker_with_two_names_routes_multihop(marker: str) -> None:
    """A relational marker paired with two mid-sentence proper nouns names the MULTIHOP route."""
    assert QueryRoute.classify(f"how is Alice {marker} Bob") == QueryRoute.MULTIHOP


def test_pointed_lookup_falls_to_local() -> None:
    """A pointed one-entity lookup with no thematic or relational marker stays LOCAL."""
    assert QueryRoute.classify("when did Alice ship") == QueryRoute.LOCAL


def test_plan_for_a_multihop_query_runs_only_the_ppr_lane() -> None:
    """A MULTIHOP query plans the personalized-pagerank lane on and the summary lanes off."""
    plan = QueryRoute.plan("how is Alice related to Bob")
    assert plan.route == QueryRoute.MULTIHOP
    assert plan.ppr == settings.ppr and not plan.communities and not plan.raptor


def test_relational_marker_with_one_name_stays_local() -> None:
    """A relational marker needs two named entities; with one it is not multi-hop."""
    assert QueryRoute.classify("what is Alice related to") == QueryRoute.LOCAL


@given(query=st.text(max_size=30))
def test_thematic_and_few_names_is_thematic(query: str) -> None:
    """`is_thematic` is true for a marker or at most one proper noun, matching the GLOBAL rule."""
    proper = sum(1 for word in query.split() if word[:1].isupper())
    marker = any(m in query.casefold() for m in THEMATIC_MARKERS)
    assert QueryRoute.is_thematic(query) == (marker or proper <= 1)


@given(query=st.text(max_size=40))
def test_plan_matches_classify_and_never_widens(query: str) -> None:
    """The route plan turns on only its route's lanes, never a lane a global toggle turned off."""
    plan = QueryRoute.plan(query)
    route = QueryRoute.classify(query)
    assert isinstance(plan, RoutePlan)
    assert plan.route == route
    match route:
        case QueryRoute.GLOBAL:
            assert plan.communities and not plan.ppr and plan.raptor == settings.raptor
        case QueryRoute.MULTIHOP:
            assert plan.ppr == settings.ppr and not plan.communities and not plan.raptor
        case QueryRoute.LOCAL:
            assert not plan.ppr and not plan.communities and not plan.raptor
