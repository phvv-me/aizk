import uuid
from datetime import UTC, datetime

from polyfactory import Use
from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import Range

from aizk.config import settings
from aizk.extract import ontology
from aizk.retrieval import Candidate
from aizk.store import (
    Community,
    Document,
    EntityClaim,
    EntityContent,
    FactContent,
    LiveFact,
    Profile,
    TableBase,
    Watermark,
)


class AizkModelFactory[T: BaseModel](ModelFactory[T]):
    __is_base_factory__ = True
    __check_model__ = False


class AizkTableFactory[T: TableBase](ModelFactory[T]):
    __is_base_factory__ = True
    __check_model__ = False
    embedding = None
    scopes: list[uuid.UUID] = [settings.system_user_id]


class EntityContentFactory(AizkTableFactory[EntityContent]):
    type = ontology.CONCEPT


class EntityClaimFactory(AizkTableFactory[EntityClaim]):
    pass


class FactContentFactory(AizkTableFactory[FactContent]):
    predicate = "related_to"


class DocumentFactory(AizkTableFactory[Document]):
    pass


class CommunityFactory(AizkTableFactory[Community]):
    pass


class ProfileFactory(AizkTableFactory[Profile]):
    pass


class WatermarkFactory(AizkTableFactory[Watermark]):
    pass


class CandidateFactory(AizkModelFactory[Candidate]):
    # polyfactory's constrained-uuid generator predates version 7, so the UUID7 row-id
    # fields get explicit providers.
    fact_id = Use(uuid.uuid7)
    source_chunk_id = Use(uuid.uuid7)


def build_live_fact(**overrides: object) -> LiveFact:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "content_id": uuid.uuid4(),
        "subject_id": uuid.uuid4(),
        "object_id": None,
        "predicate": "related_to",
        "statement": "a statement",
        "embedding": None,
        "created_by": uuid.uuid4(),
        "scopes": [settings.system_user_id],
        "valid": None,
        "recorded": Range(datetime.now(UTC), None),
        "last_accessed": None,
        "access_count": 0,
        "attributes": {},
        "perspective_key": "world",
        "source_chunk_id": None,
        "promoted_from": None,
    }
    defaults.update(overrides)
    return LiveFact(**defaults)
