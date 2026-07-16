import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from time import perf_counter

import httpx
from loguru import logger
from openai import OpenAIError
from patos import FrozenModel
from pydantic import UUID5
from sqlalchemy import NullPool, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from .background.queue import install_queue_schema
from .background.status import TasksStatus, tasks_overview
from .config import settings
from .ontology import Ontology
from .retrieval import ContextPack, recall
from .store import Chunk, Document, Entity, Fact, Profile, TableBase, verify_rls
from .store.ddl import CreateExtension, Grant, GrantTarget
from .store.identity import User

# Owner-side health counts for the store's principal tables
_MAIN_TABLES = (
    "document",
    "chunk",
    "entity_content",
    "entity_claim",
    "fact_content",
    "fact_claim",
    "community",
    "profile",
    "session_item",
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
    """Compare the live Alembic revision with the sole revision packaged by this build."""

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
    recall: RecallHealth | None
    duration_ms: float


def alembic_config() -> Config:
    """Build the alembic Config pointed at the migration scripts shipped inside the package."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "store" / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.admin_database_url)
    return config


def run_alembic[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run one blocking alembic `command` call on a dedicated worker thread, returning its
    result."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


def alembic_head(config: Config) -> str:
    """The revision the installed package's own migration scripts consider current."""
    return ScriptDirectory.from_config(config).get_current_head() or "head"


async def alembic_current() -> str | None:
    """The revision the live database is stamped at, null on a fresh unmigrated database."""
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(
                lambda sync: MigrationContext.configure(sync).get_current_revision()
            )
    finally:
        await admin.dispose()


async def queue_schema_present() -> bool:
    """Whether the pgqueuer tables already exist, setup's own idempotency probe."""
    app = create_async_engine(settings.database_url)
    try:
        async with app.connect() as connection:
            return await connection.run_sync(lambda sync: inspect(sync).has_table("pgqueuer"))
    finally:
        await app.dispose()


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
    try:
        async with admin.connect() as connection:
            counts = (
                await connection.execute(
                    select(
                        *(
                            select(func.count())
                            .select_from(TableBase.metadata.tables[table])
                            .scalar_subquery()
                            .label(table)
                            for table in _MAIN_TABLES
                        )
                    )
                )
            ).one()
            return dict(zip(_MAIN_TABLES, counts, strict=True))
    finally:
        await admin.dispose()


async def corpus_health() -> list[ScopeHealth]:
    """Read per-creator and per-scope corpus size, graph progress, and freshness in one query."""
    corpora = (
        select(
            Document.scopes.label("scopes"),
            func.count(func.distinct(Document.created_by)).label("creators"),
            func.count(Document.id).label("documents"),
            func.max(Document.updated_at).label("last_write_at"),
        )
        .group_by(Document.scopes)
        .subquery()
    )
    chunks = (
        select(func.count(Chunk.id))
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Document.scopes == corpora.c.scopes,
        )
        .correlate(corpora)
        .scalar_subquery()
    )
    processed = (
        select(func.count(Chunk.id))
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Document.scopes == corpora.c.scopes,
            Chunk.processed_at.is_not(None),
        )
        .correlate(corpora)
        .scalar_subquery()
    )
    last_projection = (
        select(func.max(Chunk.processed_at))
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Document.scopes == corpora.c.scopes,
        )
        .correlate(corpora)
        .scalar_subquery()
    )
    statement = select(
        corpora.c.scopes,
        corpora.c.creators,
        corpora.c.documents,
        chunks.label("chunks"),
        processed.label("processed_chunks"),
        select(func.count(Entity.Claim.id))
        .where(
            Entity.Claim.scopes == corpora.c.scopes,
        )
        .correlate(corpora)
        .scalar_subquery()
        .label("entities"),
        select(func.count(Fact.Claim.id))
        .where(
            Fact.Claim.scopes == corpora.c.scopes,
        )
        .correlate(corpora)
        .scalar_subquery()
        .label("facts"),
        select(func.count(Profile.id))
        .where(
            Profile.scopes == corpora.c.scopes,
        )
        .correlate(corpora)
        .scalar_subquery()
        .label("profiles"),
        corpora.c.last_write_at,
        last_projection.label("last_projection_at"),
    ).order_by(corpora.c.documents.desc())
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            rows = (await connection.execute(statement)).all()
        return [ScopeHealth.model_validate(row, from_attributes=True) for row in rows]
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
        reachable = response.status_code < 500
        payload = response.json()
        model = (
            payload.get("model") or payload.get("checkpoint")
            if isinstance(payload, dict)
            else None
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        served_as = None
        context_tokens = None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            served_as = data[0].get("id")
            model = data[0].get("root") or served_as
            context_tokens = data[0].get("max_model_len")
        model = model if isinstance(model, str) else None
        served_as = served_as if isinstance(served_as, str) else None
        context_tokens = context_tokens if isinstance(context_tokens, int) else None
    except httpx.HTTPError:
        reachable = False
        model = None
        served_as = None
        context_tokens = None
    return EndpointHealth(
        name=name,
        url=url,
        reachable=reachable,
        model=model,
        served_as=served_as,
        configured_as=configured_as,
        matched=(configured_as == served_as if configured_as and served_as else None),
        context_tokens=context_tokens,
    )


async def recall_health(corpus: ScopeHealth) -> RecallHealth:
    """Run one bounded real retrieval over the largest stored corpus."""
    started = perf_counter()
    try:
        async with asyncio.timeout(_RECALL_PROBE_TIMEOUT):
            candidates = await recall(
                _RECALL_PROBE_QUERY,
                User.system(corpus.scopes),
                token_budget=512,
            )
        pack = ContextPack.from_candidates(candidates)
        return RecallHealth(
            query=_RECALL_PROBE_QUERY,
            scopes=corpus.scopes,
            candidates=len(candidates),
            top_source=candidates[0].source_title if candidates else None,
            sample=pack.text[:500],
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


async def grant_app_role_privileges() -> None:
    """Grant the app role CRUD on the public schema, mirroring `initdb/roles.sh` on any
    database."""
    role = settings.app_role
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            for statement in (
                Grant(GrantTarget.schema, "public", role, ("USAGE",)),
                Grant(
                    GrantTarget.all_tables,
                    "public",
                    role,
                    ("SELECT", "INSERT", "UPDATE", "DELETE"),
                ),
                Grant(
                    GrantTarget.all_sequences,
                    "public",
                    role,
                    ("USAGE", "SELECT"),
                ),
                Grant(
                    GrantTarget.default_tables,
                    "public",
                    role,
                    ("SELECT", "INSERT", "UPDATE", "DELETE"),
                ),
                Grant(
                    GrantTarget.default_sequences,
                    "public",
                    role,
                    ("USAGE", "SELECT"),
                ),
            ):
                await connection.execute(statement)
    finally:
        await admin.dispose()


async def enable_query_stats() -> None:
    """Create pg_stat_statements, tolerating a Postgres not yet restarted with the library
    loaded."""
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            await connection.execute(CreateExtension("pg_stat_statements"))
    except DBAPIError as error:
        if "shared_preload_libraries" not in str(error):
            raise
        logger.warning(
            "pg_stat_statements not yet loaded, restart Postgres with the updated "
            "shared_preload_libraries to activate it: {}",
            error,
        )
    finally:
        await admin.dispose()


async def setup() -> SetupReport:
    """Bring the database to a ready state, migrate to head, install the queue schema, grant
    CRUD."""
    before = await alembic_current()
    already_queued = await queue_schema_present()
    config = alembic_config()
    run_alembic(command.upgrade, config, "head")
    await install_queue_schema()
    await grant_app_role_privileges()
    await enable_query_stats()
    async with User.system() as session:
        await Ontology.refresh(session)
    return SetupReport(
        migrated_from=before, migrated_to=alembic_head(config), queue_installed=not already_queued
    )


async def reset() -> ResetReport:
    """Recreate only the configured Aizk database, then install its complete schema."""
    name = settings.db_name
    identifier = '"' + name.replace('"', '""') + '"'
    maintenance = make_url(settings.admin_database_url).set(database="postgres")
    admin = create_async_engine(
        maintenance,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {identifier} WITH (FORCE)"))
            await connection.execute(text(f"CREATE DATABASE {identifier}"))
    finally:
        await admin.dispose()
    report = await setup()
    return ResetReport(database=name, migrated_to=report.migrated_to)


async def health() -> HealthReport:
    """Read one bounded operational and end-to-end regression snapshot."""
    started = perf_counter()
    head = alembic_head(alembic_config())
    current_task = asyncio.create_task(alembic_current())
    violations_task = asyncio.create_task(scoped_rls_violations())
    counts_task = asyncio.create_task(row_counts())
    queue_task = asyncio.create_task(tasks_overview())
    corpora_task = asyncio.create_task(corpus_health())
    endpoint_tasks = tuple(
        asyncio.create_task(probe_endpoint(name, url, path, configured_as))
        for name, url, path, configured_as in _SERVING_ENDPOINTS
    )
    current = await current_task
    violations = await violations_task
    counts = await counts_task
    queue = await queue_task
    corpora = await corpora_task
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
        recall=recall_report,
        duration_ms=round((perf_counter() - started) * 1000, 1),
    )
