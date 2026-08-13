import pytest

from aizk.config import settings
from aizk.integrations.web import SearchLane
from aizk.web import lane_providers


@pytest.mark.parametrize(
    ("lane", "expected"),
    [
        (SearchLane.keyword, settings.web_search_keyword_providers),
        (SearchLane.semantic, settings.web_search_semantic_providers),
        (SearchLane.none, ()),
    ],
)
def test_each_lane_draws_from_its_own_configured_chain(
    lane: SearchLane, expected: tuple[str, ...]
) -> None:
    assert lane_providers(settings, kind=lane) == expected


def test_a_dispatch_with_no_lane_at_all_names_no_provider() -> None:
    assert lane_providers(settings) == ()


def test_the_default_chains_are_the_measured_ones() -> None:
    assert settings.web_search_keyword_providers == ("firecrawl",)
    assert settings.web_search_semantic_providers == ("exa",)
    assert settings.web_search_fetch_providers == ("firecrawl-reader",)
