import pytest

from aizk.retrieval.models.enums import Plan
from aizk.retrieval.models.lane import Lane

_MANDATORY_ORDER = (
    Lane.Kind.FACTS,
    Lane.Kind.SOURCES,
    Lane.Kind.WORKING_MEMORY,
)


def test_plan_rejects_duplicate_order_entries() -> None:
    with pytest.raises(
        ValueError,
        match="plan order contains duplicate lane kinds: facts",
    ):
        Plan(
            order=(*_MANDATORY_ORDER, Lane.Kind.FACTS),
            profiles=False,
        )


@pytest.mark.parametrize(
    "missing",
    _MANDATORY_ORDER,
    ids=lambda kind: kind.value,
)
def test_plan_requires_every_mandatory_lane_kind(missing: Lane.Kind) -> None:
    order = tuple(kind for kind in _MANDATORY_ORDER if kind != missing)

    with pytest.raises(
        ValueError,
        match=rf"plan order is missing required lane kinds: {missing.value}",
    ):
        Plan(order=order, profiles=False)


@pytest.mark.parametrize(
    ("profiles", "communities", "raptor", "missing"),
    (
        (True, False, False, Lane.Kind.PROFILE),
        (False, True, False, Lane.Kind.COMMUNITIES),
        (False, False, True, Lane.Kind.OVERVIEW),
    ),
)
def test_plan_requires_every_enabled_lane_kind(
    profiles: bool,
    communities: bool,
    raptor: bool,
    missing: Lane.Kind,
) -> None:
    with pytest.raises(
        ValueError,
        match=rf"plan order is missing required lane kinds: {missing.value}",
    ):
        Plan(
            order=_MANDATORY_ORDER,
            profiles=profiles,
            communities=communities,
            raptor=raptor,
        )


def test_plan_allows_disabled_lane_kinds_to_be_absent() -> None:
    plan = Plan(order=_MANDATORY_ORDER, profiles=False)

    assert plan.order == _MANDATORY_ORDER
