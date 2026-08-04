from patos import value_dispatch

from ..config import Settings
from ..integrations.web import SearchLane


@value_dispatch
def lane_providers(config: Settings, **routing: SearchLane) -> tuple[str, ...]:
    """The ordered provider chain one lane draws from, keyed by the router's lane.

    A dispatch with no lane at all names no provider, which is how a call that decided
    against egress is spelled without a branch anywhere else.

    routing: carries the `kind` keyword the dispatcher pops before choosing a chain. The
        registered chains drop it, since by the time one runs the choice is already made.
    """
    del config, routing
    return ()


@lane_providers.register(SearchLane.keyword)
def keyword_providers(config: Settings) -> tuple[str, ...]:
    """The keyword chain, for questions with exact terms a text index will match."""
    return config.web_search_keyword_providers


@lane_providers.register(SearchLane.semantic)
def semantic_providers(config: Settings) -> tuple[str, ...]:
    """The semantic chain, for descriptive questions no keyword index answers well."""
    return config.web_search_semantic_providers


@lane_providers.register(SearchLane.none)
def no_providers(config: Settings) -> tuple[str, ...]:
    """An explicit none is the same refusal as no lane at all."""
    del config
    return ()
