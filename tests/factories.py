import uuid
from datetime import UTC, datetime

from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import Range

from aizk.retrieval import FactHit, Hit, RecallResult
from aizk.store import FactContent, LiveFact, TableBase


class AizkModelFactory[T: BaseModel](ModelFactory[T]):
    """Base pydantic factory for the frozen result types, opting out of the legacy model check."""

    __is_base_factory__ = True
    __check_model__ = False


class AizkTableFactory[T: TableBase](ModelFactory[T]):
    """Base factory over SQLModel table classes, pydantic-driven since a table model is its own
    pydantic schema as much as it is a mapped ORM class.

    embedding stays null on every embedded table, matching how a fresh row starts unembedded until
    the embedder fills it, rather than a random-width float list a generic strategy would build
    with no notion of the halfvec column's configured dimension. Relationships never reach a
    pydantic field in the first place, so a transient instance never pulls a whole object graph
    into being the way a naive recursive builder could.
    """

    __is_base_factory__ = True
    __check_model__ = False
    embedding = None


class FactContentFactory(AizkTableFactory[FactContent]):
    """Builds a transient `FactContent`, the deduplicated, content-addressed graph edge.

    The predicate is pinned to an ontology relation since `FactContent.predicate` now validates
    against the closed vocabulary, so a randomly generated string would be rejected at
    construction.
    """

    predicate = "related_to"


def build_live_fact(**overrides: object) -> LiveFact:
    """Build a transient `LiveFact`, the read-only `fact_claim` x `fact_content` join stand-in.

    `LiveFact` maps imperatively onto a view with no pydantic schema of its own (`store/models/
    live_fact.py`), so it carries no polyfactory `ModelFactory`; this direct constructor call,
    the plain keyword-argument `__init__` SQLAlchemy's imperative mapping installs, is the
    substitute, defaulting every field a caller does not override to a value valid for the closed
    predicate vocabulary and the live-claim shape (an open `recorded`, a stamped `reviewed_at`).

    overrides: fields to set instead of the default, keyed by `LiveFact`'s own attribute names.
    """
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "content_id": uuid.uuid4(),
        "subject_id": uuid.uuid4(),
        "object_id": None,
        "predicate": "related_to",
        "statement": "a statement",
        "embedding": None,
        "owner_id": uuid.uuid4(),
        "scope": None,
        "valid": None,
        "recorded": Range(datetime.now(UTC), None),
        "reviewed_at": datetime.now(UTC),
        "last_accessed": None,
        "access_count": 0,
        "attributes": {},
        "source_chunk_id": None,
        "promoted_from": None,
    }
    defaults.update(overrides)
    return LiveFact(**defaults)


class HitFactory(AizkModelFactory[Hit]):
    """Builds a `Hit`, one fused chunk result."""


class FactHitFactory(AizkModelFactory[FactHit]):
    """Builds a `FactHit`, one time-stamped graph result."""


class RecallResultFactory(AizkModelFactory[RecallResult]):
    """Builds a `RecallResult`, the single fused recall surface."""
