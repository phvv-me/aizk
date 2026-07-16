from datetime import UTC, datetime

from id_factory import uuid5, uuid7
from polyfactory import Use
from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import Range

from aizk.config import settings
from aizk.retrieval import Candidate
from aizk.store import Fact


class AizkModelFactory[T: BaseModel](ModelFactory[T]):
    __is_base_factory__ = True
    __check_model__ = False


class CandidateFactory(AizkModelFactory[Candidate]):
    # polyfactory's constrained-uuid generator predates version 7, so the UUID7 row-id
    # fields get explicit providers.
    fact_id = Use(uuid7)
    source_chunk_id = Use(uuid7)
    evidence_id = Use(uuid7)
    created_by = Use(uuid5)


class LiveFactFactory(AizkModelFactory[Fact.Live]):
    id = Use(uuid7)
    content_id = Use(uuid5)
    subject_id = Use(uuid5)
    object_id = None
    predicate = "related_to"
    statement = "a statement"
    embedding = None
    created_by = Use(uuid5)
    scopes = [settings.system_user_id]
    valid = None
    recorded = Use(lambda: Range(datetime.now(UTC), None))
    last_accessed = None
    access_count = 0
    attributes = {}
    perspective_key = "world"
    source_chunk_id = None
    promoted_from = None
