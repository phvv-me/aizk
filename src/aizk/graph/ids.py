import uuid

from pydantic import UUID5

from .naming import normalize_name

_NAMESPACE = uuid.UUID("a12c0de0-0000-5000-8000-a12c00000000")
_DELIMITER = "\x1f"


def normalize(value: str) -> str:
    """Fold a field to its canonical form before hashing, lowercased with collapsed
    whitespace."""
    return " ".join(value.split()).casefold()


def entity_id(name: str, type: str) -> UUID5:
    """Deterministic uuid5 for an entity from its normalized name and type."""
    fields = _DELIMITER.join((normalize(type), normalize_name(name)))
    return uuid.uuid5(_NAMESPACE, fields)


def fact_id(
    subject_id: UUID5,
    predicate: str,
    object_id: UUID5 | None,
    statement: str,
) -> UUID5:
    """Derive fact identity from resolved endpoints, predicate, and statement."""
    parts = (
        str(subject_id),
        normalize(predicate),
        str(object_id or ""),
        normalize(statement),
    )
    return uuid.uuid5(_NAMESPACE, _DELIMITER.join(parts))
