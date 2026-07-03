import uuid

from .naming import normalize_name

# fixed namespace so identities are content-addressed and stable across processes and machines.
# Two runs over the same normalized content mint the same uuid, which makes ingestion idempotent
# and the graph deterministic.
#
# DEFER full batch-invariant determinism attaches here, pinning the embedder and LLM kernels so
# even the embeddings and extraction are bit-identical run to run, not only these content ids.
NAMESPACE = uuid.UUID("a12c0de0-0000-5000-8000-a12c00000000")


# field delimiter that cannot appear inside a normalized field, so distinct field tuples never
# collide onto the same hashed string. The unit separator is invisible and absent from any name.
DELIMITER = "\x1f"


def normalize(value: str) -> str:
    """Fold a field to its canonical form before hashing, lowercased with collapsed whitespace.

    value: raw surface text of a name, type, predicate, or statement.
    """
    return " ".join(value.split()).casefold()


def entity_id(name: str, type: str) -> uuid.UUID:
    """Deterministic uuid5 for an entity from its normalized name and type.

    name: entity surface form, slug-folded before hashing so a kebab token and its spaced
        wording mint the same id.
    type: ontology entity type, normalized before hashing.
    """
    return uuid.uuid5(NAMESPACE, DELIMITER.join((normalize(type), normalize_name(name))))


def fact_id(subject: str, predicate: str, object_: str, statement: str) -> uuid.UUID:
    """Deterministic uuid5 for a fact from its normalized triple and statement.

    subject: subject entity name, normalized before hashing.
    predicate: ontology relation type, normalized before hashing.
    object_: object entity name, normalized before hashing.
    statement: self-contained fact text, normalized before hashing.
    """
    parts = (normalize(subject), normalize(predicate), normalize(object_), normalize(statement))
    return uuid.uuid5(NAMESPACE, DELIMITER.join(parts))
