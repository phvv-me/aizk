import asyncio
from time import perf_counter

import httpx
from openai import OpenAIError
from patos import FrozenModel
from pydantic import JsonValue, ValidationError
from sqlalchemy import func, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import select

from ..background.status import tasks_overview
from ..config import settings
from ..retrieval import RecallResult, recall
from ..store import (
    Artifact,
    Blob,
    Chunk,
    Document,
    Entity,
    Fact,
    Profile,
    TableBase,
    Usage,
    verify_rls,
)
from ..store.identity import User
from .provision import alembic_config, alembic_current, alembic_head
from .reports import (
    ActorUsage,
    EndpointHealth,
    ExtractionHealth,
    HealthReport,
    IdentityHealth,
    RecallHealth,
    SchemaHealth,
    ScopeHealth,
    ScopeStorage,
    ScopeUsage,
    StorageHealth,
)

# Owner-side health counts for the store's principal tables
_MAIN_TABLES = (
    "document",
    "artifact",
    "artifact_content",
    "blob",
    "chunk",
    "entity_content",
    "entity_claim",
    "fact_content",
    "fact_claim",
    "community",
    "profile",
    "session_item",
    "usage_event",
)

_SERVING_ENDPOINTS = (
    ("embed", settings.embed_url, "models", settings.embed_model),
    ("llm", settings.llm_url, "models", settings.llm_model),
    ("rerank", settings.rerank_url, "v1/models", settings.rerank_model),
    ("gliner", settings.gliner_url, "health", None),
)

_PROBE_TIMEOUT = 2.0
_RECALL_PROBE_QUERY = "What are the current active projects and their next actions?"
_RECALL_PROBE_TIMEOUT = 3.5


class ServedEntry(FrozenModel):
    """One OpenAI-style model listing entry with its optional alias and context length."""

    id: str | None = None
    root: str | None = None
    max_model_len: int | None = None


class ServingIdentity(FrozenModel):
    """The model identity a serving endpoint reports on its metadata path."""

    model: str | None = None
    checkpoint: str | None = None
    data: tuple[ServedEntry, ...] = ()

    @classmethod
    def decode(cls, payload: JsonValue) -> ServingIdentity:
        """Read any supported metadata shape, treating everything else as anonymous."""
        try:
            return cls.model_validate(payload)
        except ValidationError:
            return cls()

    def endpoint_health(
        self, name: str, url: str, reachable: bool, configured_as: str | None
    ) -> EndpointHealth:
        """Fold the decoded identity and the configured expectation into one report row."""
        entry = self.data[0] if self.data else None
        served_as = entry.id if entry else None
        model = entry.root or served_as if entry else self.model or self.checkpoint
        return EndpointHealth(
            name=name,
            url=url,
            reachable=reachable,
            model=model,
            served_as=served_as,
            configured_as=configured_as,
            matched=configured_as == served_as if configured_as and served_as else None,
            context_tokens=entry.max_model_len if entry else None,
        )


async def scoped_rls_violations() -> list[str]:
    """Reasons the live schema fails the no-leak contract for any registered scoped table."""
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(verify_rls)
    finally:
        await admin.dispose()


async def row_counts() -> dict[str, int]:
    """Read every principal table count in one owner-side SQLAlchemy statement."""
    admin = create_async_engine(settings.admin_database_url)
    counts = tuple(
        select(func.count())
        .select_from(TableBase.metadata.tables[table])
        .scalar_subquery()
        .label(table)
        for table in _MAIN_TABLES
    )
    statement = select(counts[0], counts[1], counts[2], counts[3]).add_columns(*counts[4:])
    try:
        async with admin.connect() as connection:
            row = (await connection.execute(statement)).one()
            return dict(zip(_MAIN_TABLES, map(int, row), strict=True))
    finally:
        await admin.dispose()


async def corpus_health() -> list[ScopeHealth]:
    """Read per-creator and per-scope corpus size, graph progress, and freshness in one query."""
    corpora = (
        select(
            Document.scopes.label("scopes"),
            Document.created_by.count(distinct=True).label("creators"),
            Document.id.count().label("documents"),
            Document.updated_at.max().label("last_write_at"),
        )
        .group_by(Document.scopes)
        .subquery()
    )
    chunks = (
        select(Chunk.id.count())
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Document.scopes == corpora.c.scopes,
        )
        .correlate(corpora)
        .scalar_subquery()
    )
    processed = (
        select(Chunk.id.count())
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Document.scopes == corpora.c.scopes,
            Chunk.processed_at.is_not(None),
        )
        .correlate(corpora)
        .scalar_subquery()
    )
    last_projection = (
        select(Chunk.processed_at.max())
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Document.scopes == corpora.c.scopes,
        )
        .correlate(corpora)
        .scalar_subquery()
    )
    entities = (
        select(Entity.Claim.id.count())
        .where(
            Entity.Claim.scopes == corpora.c.scopes,
        )
        .correlate(corpora)
        .scalar_subquery()
        .label("entities")
    )
    facts = (
        select(Fact.Claim.id.count())
        .where(
            Fact.Claim.scopes == corpora.c.scopes,
        )
        .correlate(corpora)
        .scalar_subquery()
        .label("facts")
    )
    profiles = (
        select(Profile.id.count())
        .where(
            Profile.scopes == corpora.c.scopes,
        )
        .correlate(corpora)
        .scalar_subquery()
        .label("profiles")
    )
    statement = (
        select(
            corpora.c.scopes,
            corpora.c.creators,
            corpora.c.documents,
            chunks.label("chunks"),
        )
        .add_columns(
            processed.label("processed_chunks"),
            entities,
            facts,
            profiles,
            corpora.c.last_write_at,
            last_projection.label("last_projection_at"),
        )
        .order_by(corpora.c.documents.desc())
    )
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            rows = (await connection.execute(statement)).all()
        return [ScopeHealth.model_validate(row, from_attributes=True) for row in rows]
    finally:
        await admin.dispose()


async def usage_health() -> tuple[
    list[ActorUsage],
    list[ScopeUsage],
    list[ScopeStorage],
    StorageHealth,
]:
    """Read actor operations, target operations, scope storage, and physical storage costs."""
    targets = Usage.Event.targets.f.unnest().table_valued("scope_id").render_derived()
    actors = select(Usage.Event.created_by.label("actor_id"), *Usage.Event.aggregate()).group_by(
        Usage.Event.created_by
    )
    scopes = (
        select(targets.c.scope_id, *Usage.Event.aggregate())
        .select_from(Usage.Event)
        .join(targets, true())
        .group_by(targets.c.scope_id)
    )
    logical = (
        select(
            Artifact.Content.id.count().label("originals"),
            Blob.size.sum(default=0).label("logical_bytes"),
        )
        .join(Blob, Blob.id == Artifact.Content.blob_id)
        .subquery()
    )
    physical = (
        select(
            Blob.id.count().label("physical_blobs"),
            Blob.size.sum(default=0).label("original_bytes"),
            Blob.stored_size.sum(default=0).label("stored_bytes"),
            Blob.id.count().filter(Blob.integrity_checked_at.is_(None)).label("unverified_blobs"),
        )
        .add_columns(
            Blob.id.count()
            .filter(Blob.integrity_error.is_not(None))
            .label("failed_integrity_blobs"),
            Blob.integrity_checked_at.max().label("last_integrity_check"),
        )
        .subquery()
    )
    storage = (
        select(
            logical.c.originals,
            logical.c.logical_bytes,
            physical.c.physical_blobs,
            physical.c.original_bytes,
        )
        .add_columns(
            physical.c.stored_bytes,
            (physical.c.original_bytes - physical.c.stored_bytes).label("compression_saved_bytes"),
            physical.c.unverified_blobs,
            physical.c.failed_integrity_blobs,
            physical.c.last_integrity_check,
        )
        .select_from(logical.join(physical, true()))
    )
    scope_storage = (
        select(
            Artifact.Content.scopes,
            Artifact.Content.id.count().label("artifact_revisions"),
            Blob.size.sum(default=0).label("logical_bytes"),
        )
        .join(Blob, Blob.id == Artifact.Content.blob_id)
        .group_by(Artifact.Content.scopes)
    )
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            actor_rows = (await connection.execute(actors)).all()
            scope_rows = (await connection.execute(scopes)).all()
            scope_storage_rows = (await connection.execute(scope_storage)).all()
            storage_row = (await connection.execute(storage)).one()
        return (
            [ActorUsage.model_validate(row, from_attributes=True) for row in actor_rows],
            [ScopeUsage.model_validate(row, from_attributes=True) for row in scope_rows],
            [ScopeStorage.model_validate(row, from_attributes=True) for row in scope_storage_rows],
            StorageHealth.model_validate(storage_row, from_attributes=True),
        )
    finally:
        await admin.dispose()


async def probe_endpoint(
    name: str,
    url: str,
    path: str = "models",
    configured_as: str | None = None,
) -> EndpointHealth:
    """Probe one serving endpoint path with a short timeout."""
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            response = await client.get(f"{url.rstrip('/')}/{path.lstrip('/')}")
    except httpx.HTTPError:
        return EndpointHealth(name=name, url=url, reachable=False, configured_as=configured_as)
    try:
        identity = ServingIdentity.decode(response.json())
    except ValueError:
        identity = ServingIdentity()
    return identity.endpoint_health(name, url, response.status_code < 500, configured_as)


async def recall_health(corpus: ScopeHealth) -> RecallHealth:
    """Run one bounded real retrieval over the largest stored corpus."""
    started = perf_counter()
    try:
        async with asyncio.timeout(_RECALL_PROBE_TIMEOUT):
            candidates = await recall(
                _RECALL_PROBE_QUERY,
                User.system(corpus.scopes),
                k=2,
                token_budget=512,
            )
        result = RecallResult.from_candidates(candidates)
        return RecallHealth(
            query=_RECALL_PROBE_QUERY,
            scopes=corpus.scopes,
            candidates=len(candidates),
            top_source=candidates[0].source_title if candidates else None,
            sample=(await result.to_markdown())[:500],
            latency_ms=round((perf_counter() - started) * 1000, 1),
        )
    except (TimeoutError, httpx.HTTPError, OpenAIError, DBAPIError) as error:
        return RecallHealth(
            query=_RECALL_PROBE_QUERY,
            scopes=corpus.scopes,
            candidates=0,
            top_source=None,
            sample="",
            latency_ms=round((perf_counter() - started) * 1000, 1),
            error=f"{type(error).__name__}: {error}"[:300],
        )


async def health() -> HealthReport:
    """Read one bounded operational and end-to-end regression snapshot."""
    started = perf_counter()
    head = alembic_head(alembic_config())
    current_task = asyncio.create_task(alembic_current())
    violations_task = asyncio.create_task(scoped_rls_violations())
    counts_task = asyncio.create_task(row_counts())
    queue_task = asyncio.create_task(tasks_overview())
    corpora_task = asyncio.create_task(corpus_health())
    usage_task = asyncio.create_task(usage_health())
    endpoint_tasks = tuple(
        asyncio.create_task(probe_endpoint(name, url, path, configured_as))
        for name, url, path, configured_as in _SERVING_ENDPOINTS
    )
    current = await current_task
    violations = await violations_task
    counts = await counts_task
    queue = await queue_task
    corpora = await corpora_task
    actors, scopes, scope_storage, storage = await usage_task
    endpoints = [await task for task in endpoint_tasks]
    recall_report = await recall_health(corpora[0]) if corpora else None
    return HealthReport(
        migration=SchemaHealth(current=current, head=head, up_to_date=current == head),
        rls_violations=violations,
        row_counts=counts,
        queue=queue,
        endpoints=endpoints,
        extraction=ExtractionHealth(
            backend=settings.extract_backend,
            window_chars=settings.extract_window_size,
            output_tokens=settings.llm_extract_max_tokens,
        ),
        identity=IdentityHealth(
            mode="logto" if settings.logto_url is not None else "local",
            public_url=(
                str(settings.mcp_public_url) if settings.mcp_public_url is not None else None
            ),
        ),
        corpora=corpora,
        actors=actors,
        scopes=scopes,
        scope_storage=scope_storage,
        storage=storage,
        recall=recall_report,
        duration_ms=round((perf_counter() - started) * 1000, 1),
    )
