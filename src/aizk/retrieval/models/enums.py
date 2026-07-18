from typing import Self

from patos import FrozenModel
from pydantic import model_validator

from ...config import settings
from ..lanes import (
    EntityCatalogLane,
    FactLane,
    OverviewLane,
    SourceLane,
    VectorLane,
)
from .lane import Lane

_overview_first = (
    Lane.Kind.OVERVIEW,
    Lane.Kind.COMMUNITIES,
    Lane.Kind.FACTS,
    Lane.Kind.SOURCES,
    Lane.Kind.PROFILE,
    Lane.Kind.WORKING_MEMORY,
)
_facts_first = (
    Lane.Kind.FACTS,
    Lane.Kind.SOURCES,
    Lane.Kind.WORKING_MEMORY,
    Lane.Kind.PROFILE,
    Lane.Kind.OVERVIEW,
    Lane.Kind.COMMUNITIES,
)


class Plan(FrozenModel):
    """One declarative retrieval shape, the lane order plus the graph toggles.

    Production always runs `maximal`; the narrower presets survive as the eval plan
    study's comparison arms. New retrieval behavior becomes a new Plan value rather
    than new query code, and the recall statement caches per plan since equal plans
    compile identical SQL.
    """

    order: tuple[Lane.Kind, ...]
    communities: bool = False
    raptor: bool = False
    profiles: bool = True
    hops: int = 0

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        """Reject orders that cannot supply every lane the plan will instantiate."""
        if len(self.order) != len(set(self.order)):
            duplicates = sorted({kind.value for kind in self.order if self.order.count(kind) > 1})
            raise ValueError("plan order contains duplicate lane kinds: " + ", ".join(duplicates))
        required = {
            Lane.Kind.FACTS,
            Lane.Kind.SOURCES,
            Lane.Kind.WORKING_MEMORY,
        }
        required.update(
            kind
            for enabled, kind in (
                (self.profiles, Lane.Kind.PROFILE),
                (self.communities, Lane.Kind.COMMUNITIES),
                (self.raptor, Lane.Kind.OVERVIEW),
            )
            if enabled
        )
        missing = sorted(kind.value for kind in required.difference(self.order))
        if missing:
            raise ValueError("plan order is missing required lane kinds: " + ", ".join(missing))
        return self

    @classmethod
    def maximal(cls) -> Plan:
        """The production plan, every lane on in facts-first order with the configured
        hops, read fresh so a changed setting takes effect immediately."""
        return cls(
            order=_facts_first,
            communities=True,
            raptor=True,
            hops=settings.multihop_max_hops,
        )

    @classmethod
    def maximal_without_raptor(cls) -> Plan:
        """The maximal plan without RAPTOR overview recall."""
        return cls.maximal().model_copy(update={"raptor": False})

    @classmethod
    def maximal_without_communities(cls) -> Plan:
        """The maximal plan without community-summary recall."""
        return cls.maximal().model_copy(update={"communities": False})

    @classmethod
    def maximal_without_profiles(cls) -> Plan:
        """The maximal plan without entity-profile recall."""
        return cls.maximal().model_copy(update={"profiles": False})

    @classmethod
    def focused(cls) -> Plan:
        """The historical LOCAL shape, facts first with no graph overviews or hops."""
        return cls(order=_facts_first)

    @classmethod
    def overview(cls) -> Plan:
        """The historical GLOBAL shape, graph overviews first with communities and
        RAPTOR on."""
        return cls(order=_overview_first, communities=True, raptor=True)

    @classmethod
    def multihop(cls) -> Plan:
        """The historical MULTIHOP shape, facts first with the configured graph hops."""
        return cls(order=_facts_first, hops=settings.multihop_max_hops)

    @property
    def lanes(self) -> tuple[Lane, ...]:
        """The lane instances this plan unions, prioritized in its declared order.

        The evidence lanes are always present with route-independent limits while the
        graph-overview lanes follow the plan's toggles.
        """
        priority = {kind: rank for rank, kind in enumerate(self.order)}
        lanes: list[Lane] = [
            FactLane(priority=priority[Lane.Kind.FACTS], hops=self.hops),
            SourceLane(priority=priority[Lane.Kind.SOURCES]),
            EntityCatalogLane(priority=priority[Lane.Kind.SOURCES]),
            VectorLane(kind=Lane.Kind.WORKING_MEMORY, priority=priority[Lane.Kind.WORKING_MEMORY]),
        ]
        if self.profiles:
            lanes.append(VectorLane(kind=Lane.Kind.PROFILE, priority=priority[Lane.Kind.PROFILE]))
        if self.communities:
            lanes.append(
                VectorLane(kind=Lane.Kind.COMMUNITIES, priority=priority[Lane.Kind.COMMUNITIES])
            )
        if self.raptor:
            lanes.append(OverviewLane(priority=priority[Lane.Kind.OVERVIEW]))
        return tuple(lanes)
