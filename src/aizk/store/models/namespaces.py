from .tables import (
    EntityClaim,
    EntityContent,
    EntityKind,
    FactClaim,
    FactContent,
    RelationKind,
    RelationPolicy,
)
from .views import LiveFact


class Entity:
    """Entity ontology and persistence models under one stable namespace."""

    Kind = EntityKind
    Content = EntityContent
    Claim = EntityClaim


class Fact:
    """Immutable fact content, scoped claims, and the current fact view."""

    Content = FactContent
    Claim = FactClaim
    Live = LiveFact


class Relation:
    """Relation ontology models and their coexistence policies."""

    Kind = RelationKind
    Policy = RelationPolicy
