import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.config import settings
from aizk.retrieval import QueryRoute


@pytest.mark.parametrize(
    ("query", "route"),
    [
        ("When was Alice born", QueryRoute.LOCAL),
        ("How is Alice doing", QueryRoute.LOCAL),
        ("Alice related to Bob", QueryRoute.LOCAL),
        ("Give me an overview of the project", QueryRoute.GLOBAL),
        ("Summarize the recent work", QueryRoute.GLOBAL),
        ("What is the state of Alice and Bob", QueryRoute.GLOBAL),
        ("How is Alice related to Bob", QueryRoute.MULTIHOP),
        ("Show the connection between Alice and Bob", QueryRoute.MULTIHOP),
    ],
)
def test_classify_query_routes_by_marker_and_named_entities(query: str, route: QueryRoute) -> None:
    """A thematic marker routes GLOBAL, a relational marker with two names routes MULTIHOP."""
    assert QueryRoute.classify(query) is route


@pytest.mark.parametrize(
    ("query", "thematic"),
    [
        ("Give me an overview", True),
        ("hello world", True),
        ("tell me about Alice", True),
        ("When was Alice born and where did Bob go", False),
        ("Alice and Bob", False),
    ],
)
def test_is_thematic_reads_markers_and_proper_noun_count(query: str, thematic: bool) -> None:
    """A marker or at most one proper noun reads thematic, several proper nouns read pointed."""
    assert QueryRoute.is_thematic(query) is thematic


@given(
    query=st.sampled_from(
        ["When was Alice born", "Give me an overview", "How is Alice related to Bob"]
    ),
    ppr=st.booleans(),
    raptor=st.booleans(),
)
def test_plan_route_narrows_the_mix_and_never_widens_a_disabled_lane(
    query: str, ppr: bool, raptor: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routing only ever turns a lane on when both its route and its global flag allow it."""
    monkeypatch.setattr(settings, "ppr", ppr)
    monkeypatch.setattr(settings, "raptor", raptor)
    plan = QueryRoute.plan(query)

    assert not plan.ppr or ppr
    assert not plan.raptor or raptor
    match plan.route:
        case QueryRoute.LOCAL:
            assert not (plan.ppr or plan.communities or plan.raptor)
        case QueryRoute.GLOBAL:
            assert plan.communities and not plan.ppr and plan.raptor is raptor
        case QueryRoute.MULTIHOP:
            assert plan.ppr is ppr and not (plan.communities or plan.raptor)
