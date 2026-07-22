from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from id_factory import uuid5, uuid7, uuid8
from polyfactory import Use
from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import UUID5, UUID7, BaseModel

from aizk.config import settings
from aizk.retrieval import Candidate
from aizk.storage import StoredBytes
from aizk.store import Artifact, Blob, Fact
from aizk.store.identity import User


class AizkModelFactory[T: BaseModel](ModelFactory[T]):
    __is_base_factory__ = True
    __check_model__ = False


class CandidateFactory(AizkModelFactory[Candidate]):
    # polyfactory's constrained-uuid generator predates version 7, so the UUID7 row-id
    # fields get explicit providers.
    fact_id = Use(uuid7)
    source_chunk_id = Use(uuid7)
    evidence_id = Use(uuid7)
    artifact_id = None
    artifact_content_id = None
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
    valid_from = None
    valid_to = None
    recorded_from = Use(lambda: datetime.now(UTC))
    recorded_to = None
    last_accessed = None
    access_count = 0
    attributes = {}
    perspective_key = "world"
    source_chunk_id = None
    promoted_from = None


@dataclass(frozen=True)
class StoredArtifact:
    """One persisted artifact with its blob and original content revision."""

    owner: UUID5
    blob: Blob
    artifact: Artifact
    content: Artifact.Content


def artifact_blob(
    *,
    media_type: str = "text/plain",
    size: int = 1,
    storage_key: str | None = None,
    stored: StoredBytes | None = None,
) -> Blob:
    """Build one immutable blob row, synthetic by default or mirroring already-stored bytes."""
    if stored is not None:
        return Blob(
            content_hash=stored.content_hash,
            size=stored.size,
            stored_size=stored.stored_size,
            encoding=stored.encoding,
            storage_key=stored.key,
            storage_version=stored.version,
            media_type=media_type,
            etag=stored.etag,
        )
    return Blob(
        content_hash=uuid8(),
        size=size,
        stored_size=size,
        storage_key=storage_key or f"objects/{uuid5()}",
        media_type=media_type,
    )


def artifact_content(
    artifact_id: UUID7,
    blob_id: UUID7,
    owner: UUID5,
    scopes: Sequence[UUID5],
    *,
    revision: int = 1,
    state: Artifact.Content.State = Artifact.Content.State.pending,
    created_at: datetime | None = None,
) -> Artifact.Content:
    """Build one artifact content revision bound to a blob."""
    extra = {"created_at": created_at} if created_at is not None else {}
    return Artifact.Content(
        artifact_id=artifact_id,
        blob_id=blob_id,
        revision=revision,
        state=state,
        created_by=owner,
        scopes=list(scopes),
        **extra,
    )


async def seed_artifact(
    owner: UUID5,
    scopes: Sequence[UUID5],
    *,
    name: str = "notes.txt",
    media_type: str = "text/plain",
    size: int = 1,
    storage_key: str | None = None,
    state: Artifact.Content.State = Artifact.Content.State.pending,
    created_at: datetime | None = None,
    source_uri: str | None = None,
) -> StoredArtifact:
    """Persist one original artifact revision through the scoped app role and return its rows."""
    scope_list = list(scopes)
    blob = artifact_blob(media_type=media_type, size=size, storage_key=storage_key)
    artifact = Artifact(name=name, source_uri=source_uri, created_by=owner, scopes=scope_list)
    async with User.authorized(owner, read=scope_list, write=scope_list) as session:
        session.add_all((blob, artifact))
        await session.flush()
        content = artifact_content(
            artifact.id, blob.id, owner, scope_list, state=state, created_at=created_at
        )
        session.add(content)
    return StoredArtifact(owner, blob, artifact, content)
