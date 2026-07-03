import os
import sys
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cyclopts import App
from loguru import logger
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from alembic.config import Config

from .background.queue import enqueue_pending, install_queue_schema
from .background.schedule import run_worker
from .config import settings
from .extract.ingest import ingest_text
from .retrieval import recall
from .store import Principal, TableBase, system_session, verify_scoped_rls

# env var a Stop hook sets to the session transcript path, the same file Claude Code records the
# turn-by-turn session to, so capture-session can read it without any argument
TRANSCRIPT_ENV = "AIZK_SESSION_TRANSCRIPT"

# default recall when a SessionStart hook calls recall-context with no query, broad enough to pull
# the recent decisions, patterns, and gotchas worth having loaded before a task begins
PROJECT_CONTEXT_QUERY = "recent decisions, patterns, gotchas, and project context"

app = App(
    name="aizk",
    help="Process and bootstrap entrypoint for the aizk memory engine, whose verbs are MCP tools.",
)


def alembic_config() -> Config:
    """Build the alembic Config pointed at the migration scripts shipped inside the package."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "store" / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.admin_database_url)
    return config


def run_alembic[T](fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Run one blocking alembic `command` call on a dedicated worker thread, returning its result.

    alembic's command API is synchronous, but `store/migrations/env.py` opens its DSN through an
    async engine and drives it with its own top-level `asyncio.run`, which raises when the calling
    thread already runs a loop, exactly the case a test's `asyncio.run(...)`-driven scenario hits.
    A private thread carries no loop of its own, so the alembic call is safe to make from a plain
    synchronous caller and from inside an already-running event loop alike, blocking either way
    until the migration finishes.

    fn: the alembic `command` callable to run, `command.upgrade` or `command.revision`.
    args: positional arguments forwarded to `fn`.
    kwargs: keyword arguments forwarded to `fn`.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


@app.command
def migrate() -> None:
    """Apply database migrations up to head, against the DSN `alembic_config` resolves."""
    run_alembic(command.upgrade, alembic_config(), "head")
    print("done")


@app.command
def makemigrations(message: str) -> None:
    """Autogenerate a new database migration from the current model metadata.

    Diffs the live database, which should already be at head, against the ORM metadata and writes
    the resulting revision file, the same autogenerate `alembic revision --autogenerate` runs.

    message: short description for the revision, becomes its file slug and migration docstring.
    """
    run_alembic(command.revision, alembic_config(), message=message, autogenerate=True)
    print("done")


async def scoped_rls_violations() -> list[str]:
    """Reasons the live schema fails the no-leak contract for any registered scoped table.

    Reads the catalog through the owning role, the only role that can see every table's row
    security flags and policy expressions, checking each Scoped model against `verify_scoped_rls`.
    """
    expected = set(TableBase.metadata.info["rls"])
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(lambda sync: verify_scoped_rls(sync, expected))
    finally:
        await admin.dispose()


@app.command
async def check_rls() -> None:
    """Verify every scoped table forces row level security with the canonical scope policies.

    Exits non-zero and lists each regression when a scoped table has lost ENABLE, FORCE, the
    scope_read or scope_write policy, or a clause that no longer scopes by owner and membership, so
    the no-leak contract can be gated in CI and checked by hand against any live database.
    """
    violations = await scoped_rls_violations()
    if violations:
        for reason in violations:
            print(reason)
        sys.exit(1)
    print("ok")


@app.command
async def worker(batch_size: int = 10) -> None:
    """Run the autonomous engine, the queue and the scheduler together, until interrupted.

    Drains the on-write extraction and profile jobs and fires the scheduled maintenance passes,
    decay, dedup, communities, RAPTOR, profile refresh, self-improve, session promotion, insight,
    and curation review, each fanning out one job per principal under its own row level security
    scope, so a single `aizk worker` self-maintains.

    batch_size: maximum number of jobs dequeued per round.
    """
    await run_worker(batch_size=batch_size)


@app.command
async def install_queue() -> None:
    """Install the pgqueuer schema as the owner and grant the app role access."""
    await install_queue_schema()
    print("done")


@app.command
def serve_mcp() -> None:
    """Run the aizk MCP server, the one interface through which every memory verb is reached.

    Serves stdio by default and streamable HTTP when AIZK_MCP_HTTP is set. fastmcp is imported
    lazily here so the rest of the CLI stays usable without it installed.
    """
    from .mcp.server import server

    logger.info("serving aizk mcp, http={}", settings.mcp_http)
    if settings.mcp_http:
        server.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)
    else:
        server.run()


@app.command
async def recall_context(
    query: str | None = None,
    k: int = 8,
    principal: uuid.UUID | None = None,
) -> None:
    """Recall memory and print it for a SessionStart hook to inject as context.

    This is the shell bridge the session hook calls, not a memory verb, so it stays in the CLI
    while the verbs live as MCP tools. With no query it recalls the recent project context,
    otherwise it recalls for the query, and the formatted facts and source snippets are printed to
    stdout where the hook captures them.

    query: what to recall, the recent project context when null.
    k: number of hits and seed facts to surface.
    principal: identity whose visibility scopes the recall, the system principal when null.
    """
    result = await recall(
        query or PROJECT_CONTEXT_QUERY,
        principal_id=principal or settings.system_principal_id,
        k=k,
    )
    print(result.render())


@app.command
async def capture_session(
    principal: uuid.UUID | None = None,
) -> None:
    """Capture the session's decisions into memory for a Stop hook to run at the end of a session.

    This is the shell bridge the session hook calls, not a memory verb, so it stays in the CLI
    while the verbs live as MCP tools. It reads the transcript path from the environment, remembers
    its text as a document so its decisions, patterns, and gotchas are chunked and embedded, and
    enqueues graph extraction so the extractor turns them into facts. Without a transcript path it
    is a quiet no-op, so the hook is safe to run in any session.

    principal: identity that owns the captured memory, the system principal when null.
    """
    transcript = os.environ.get(TRANSCRIPT_ENV)
    if not transcript or not Path(transcript).is_file():
        print("no session transcript to capture")
        return
    principal_id = principal or settings.system_principal_id
    text_content = Path(transcript).read_text(encoding="utf-8")

    document_id = await ingest_text(
        text_content, title=Path(transcript).stem, owner_id=principal_id
    )
    await enqueue_pending(principal_id=principal_id)
    print(f"captured session into document {document_id}")


@app.command
async def scale(
    sizes: str = "1000,10000",
    k: int = 8,
    repeats: int = 10,
    recall_p95_ms: float = 200.0,
) -> None:
    """Run the scale benchmark and print the scaling curve with the knee flagged per component.

    Grows a synthetic corpus through the sizes under a throwaway principal, measures recall latency
    percentiles with a per-lane breakdown, ingestion throughput, the pagerank and community-detect
    graph ops, and the storage footprint at each size, then prints the curve and the first size
    each component broke its budget. The throwaway principal and its rows are purged at the end.

    sizes: comma-separated corpus chunk counts to measure, the hundred-thousand point left opt-in.
    k: number of hits and seed facts each recall surfaces.
    repeats: how many recall and per-lane calls each percentile is read over.
    recall_p95_ms: the tail recall budget in milliseconds the recall knee is flagged against.
    """
    from .eval.scale import Budget, run_scale_benchmark

    report = await run_scale_benchmark(
        sizes=tuple(int(size) for size in sizes.split(",")),
        k=k,
        repeats=repeats,
        budget=Budget(recall_p95_ms=recall_p95_ms),
    )
    print(report.render())


async def create_user_principal(name: str) -> Principal:
    """Create a principal under one system-acting session, the CLI's own testable seam.

    name: human-readable display name for the new actor.
    """
    async with system_session() as session:
        return await Principal.create(session, name)


@app.command
async def create_user(name: str) -> None:
    """Create a principal and print its id, the root bootstrap that cannot require auth.

    name: human-readable display name for the new actor.
    """
    principal = await create_user_principal(name)
    print(principal.id)


if __name__ == "__main__":
    app()
