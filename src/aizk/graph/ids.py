import uuid

from .naming import normalize_name

_NAMESPACE = uuid.UUID("a12c0de0-0000-5000-8000-a12c00000000")
_DELIMITER = "\x1f"


def normalize(value: str) -> str:
    """Fold a field to its canonical form before hashing, lowercased with collapsed
    whitespace."""
    return " ".join(value.split()).casefold()


def entity_id(name: str, type: str) -> uuid.UUID:
    """Deterministic uuid5 for an entity from its normalized name and type."""
    return uuid.uuid5(_NAMESPACE, _DELIMITER.join((normalize(type), normalize_name(name))))


def fact_id(subject: str, predicate: str, object_: str, statement: str) -> uuid.UUID:
    """Deterministic uuid5 for a fact from its normalized triple and statement."""
    parts = (normalize(subject), normalize(predicate), normalize(object_), normalize(statement))
    return uuid.uuid5(_NAMESPACE, _DELIMITER.join(parts))
