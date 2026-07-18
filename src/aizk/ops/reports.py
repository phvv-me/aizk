from datetime import datetime

from patos import FrozenModel
from pydantic import UUID5

from ..background.status import TasksStatus


class SetupReport(FrozenModel):
    """Report the migration transition and whether setup first installed PgQueuer."""

    migrated_from: str | None
    migrated_to: str
    queue_installed: bool


class ResetReport(FrozenModel):
    """Identify the Aizk database recreated without touching the separate Logto database."""

    database: str
    migrated_to: str


class SchemaHealth(FrozenModel):
    """Compare the live Alembic revision with the migration head packaged by this build."""

    current: str | None
    head: str
    up_to_date: bool


class EndpointHealth(FrozenModel):
    """Describe one model endpoint's reachability, served identity, and context contract."""

    name: str
    url: str
    reachable: bool
    model: str | None = None
    served_as: str | None = None
    configured_as: str | None = None
    matched: bool | None = None
    context_tokens: int | None = None


class ExtractionHealth(FrozenModel):
    """Show the configured extraction window and output budget beside its backend."""

    backend: str
    window_chars: int
    output_tokens: int


class IdentityHealth(FrozenModel):
    """Show whether requests use Logto identity or the explicit local auth-off identity."""

    mode: str
    public_url: str | None


class ScopeHealth(FrozenModel):
    """Measure one exact scope-set corpus, its graph progress, and latest durable writes."""

    scopes: tuple[UUID5, ...]
    creators: int
    documents: int
    chunks: int
    processed_chunks: int
    entities: int
    facts: int
    profiles: int
    last_write_at: datetime
    last_projection_at: datetime | None


class RecallHealth(FrozenModel):
    """Record one bounded real recall over the largest corpus visible to its scope set."""

    query: str
    scopes: tuple[UUID5, ...]
    candidates: int
    top_source: str | None
    sample: str
    latency_ms: float
    error: str | None = None


class ActorUsage(FrozenModel):
    """Aggregate successful work and transferred bytes for one authenticated caller."""

    actor_id: UUID5
    recalls: int
    remembers: int
    files: int
    shares: int
    artifact_reads: int
    request_bytes: int
    response_bytes: int


class ScopeUsage(FrozenModel):
    """Attribute successful work to one private or organization target scope."""

    scope_id: UUID5
    recalls: int
    remembers: int
    files: int
    shares: int
    artifact_reads: int
    request_bytes: int
    response_bytes: int


class StorageHealth(FrozenModel):
    """Separate logical file references from physical object-store consumption."""

    originals: int
    logical_bytes: int
    physical_blobs: int
    original_bytes: int
    stored_bytes: int
    compression_saved_bytes: int
    unverified_blobs: int
    failed_integrity_blobs: int
    last_integrity_check: datetime | None


class ScopeStorage(FrozenModel):
    """Measure logical original-file references in one exact private or shared scope set."""

    scopes: tuple[UUID5, ...]
    artifact_revisions: int
    logical_bytes: int


class HealthReport(FrozenModel):
    """Combine schema, RLS, storage, queue, models, identity, corpora, and recall health."""

    migration: SchemaHealth
    rls_violations: list[str]
    row_counts: dict[str, int]
    queue: TasksStatus
    endpoints: list[EndpointHealth]
    extraction: ExtractionHealth
    identity: IdentityHealth
    corpora: list[ScopeHealth]
    actors: list[ActorUsage]
    scopes: list[ScopeUsage]
    scope_storage: list[ScopeStorage]
    storage: StorageHealth
    recall: RecallHealth | None
    duration_ms: float
