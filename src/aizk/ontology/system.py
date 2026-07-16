from enum import StrEnum, auto


class System:
    """Internal ontology names required by application behavior."""

    class Entity(StrEnum):
        """Entity kinds used by internal graph machinery."""

        RAPTOR_SUMMARY = auto()
        OBSERVATION = auto()
        CONCEPT = auto()

    class Relation(StrEnum):
        """Relation kinds used by internal graph machinery."""

        OBSERVES = auto()
        RELATED_TO = auto()
