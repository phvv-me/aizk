from enum import StrEnum, auto


class EntityType(StrEnum):
    """The handful of entity types referenced by name in code rather than only through the live
    catalog, always seeded, always active, never deactivatable (RAPTOR_SUMMARY and OBSERVATION
    carry `structural=True` in the seed migration for exactly that reason).

    `auto()` lowercases each member name into its snake_case catalog value, so `RAPTOR_SUMMARY` is
    exactly `raptor_summary`, the string `EntityContent.type` stores and the live catalog
    foreign-keys against. The member is a `str`, so it drops into a query or comparison unchanged.
    """

    RAPTOR_SUMMARY = auto()
    OBSERVATION = auto()
    PROJECT = auto()
    AREA = auto()
    CONCEPT = auto()


class Predicate(StrEnum):
    """The relation predicates referenced by name in code, snake_case by the same `auto()` rule,
    the string `FactContent.predicate` stores. OBSERVES is structural, the predicate every
    system-derived observation carries.
    """

    OBSERVES = auto()
    RELATED_TO = auto()
    DEPENDS_ON = auto()
    PART_OF = auto()
    CITES = auto()
    SUPERSEDES = auto()


# flat aliases so a call site reads the terse `ontology.CONCEPT` the whole codebase already uses,
# while the enums above stay the single source of every snake_case value
RAPTOR_SUMMARY, OBSERVATION, PROJECT, AREA, CONCEPT = EntityType
OBSERVES, RELATED_TO, DEPENDS_ON, PART_OF, CITES, SUPERSEDES = Predicate
