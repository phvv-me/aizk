import asyncio
import os
import sys
import uuid
from pathlib import Path

from cyclopts import App
from loguru import logger
from mainboard.profiling import enable_spans

from alembic import command

from . import backup as backup_ops
from . import ops
from .background.queue import enqueue_pending, install_queue_schema
from .background.schedule import run_worker
from .config import settings
from .extract.ingest import ingest_text
from .retrieval import assemble_context_pack
from .store import Principal, system_session

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


@app.command
def migrate() -> None:
    """Apply database migrations up to head, the pre-auth bootstrap step `ops.setup` also runs.

    A thin wrapper over `ops.run_alembic`, kept as its own command since a fresh database has no
    admin principal yet to call the MCP `setup` tool through.
    """
    ops.run_alembic(command.upgrade, ops.alembic_config(), "head")
    print("done")


@app.command
def makemigrations(message: str) -> None:
    """Autogenerate a new database migration from the current model metadata.

    Diffs the live database, which should already be at head, against the ORM metadata and writes
    the resulting revision file, the same autogenerate `alembic revision --autogenerate` runs.

    message: short description for the revision, becomes its file slug and migration docstring.
    """
    ops.run_alembic(command.revision, ops.alembic_config(), message=message, autogenerate=True)
    print("done")


@app.command
async def check_rls() -> None:
    """Verify every scoped table forces row level security with the canonical scope policies.

    Exits non-zero and lists each regression when a scoped table has lost ENABLE, FORCE, the
    scope_read or scope_write policy, or a clause that no longer scopes by owner and membership, so
    the no-leak contract can be gated in CI and checked by hand against any live database.
    """
    violations = await ops.scoped_rls_violations()
    if violations:
        for reason in violations:
            print(reason)
        sys.exit(1)
    print("ok")


@app.command
async def worker(batch_size: int = settings.queue_batch_size) -> None:
    """Run the autonomous engine, the queue and the scheduler together, until interrupted.

    Drains the on-write extraction and profile jobs and fires the scheduled maintenance passes,
    decay, dedup, communities, RAPTOR, profile refresh, self-improve, session promotion, insight,
    and curation review, each fanning out one job per principal under its own row level security
    scope, so a single `aizk worker` self-maintains.

    batch_size: maximum number of jobs dequeued per round, settings.queue_batch_size by default,
        sized above settings.graph_build_concurrency so the queue path actually keeps enough
        chunks in flight to saturate vLLM's continuous batching.
    """
    if settings.profiling:
        enable_spans()
    await run_worker(batch_size=batch_size)


@app.command
async def install_queue() -> None:
    """Install the pgqueuer schema as the owner and grant the app role access.

    A thin wrapper over the same `install_queue_schema` the MCP `setup` tool runs, kept as its own
    command for the pre-auth bootstrap case where no admin principal exists to call it through yet.
    """
    await install_queue_schema()
    print("done")


@app.command
async def backup(path: str) -> None:
    """Dump the whole database to a portable archive at `path`, the durable snapshot of memory.

    Runs `pg_dump` through `settings.pg_client_launcher` so a compose deployment captures the
    database with the container's own version-matched binaries. Schedule it from cron for the
    automated half of the backup story, `restore` reads the archive back.

    path: the host file the archive is written to.
    """
    report = await backup_ops.backup_database(path)
    print(f"backed up {report.bytes} bytes to {report.path}")


@app.command
async def restore(path: str) -> None:
    """Load a backup archive back into the configured database, overwriting its current contents.

    Destructive, `pg_restore --clean` drops each object the archive recreates, so the database
    ends holding exactly the backup. For a non-destructive recovery drill, restore into a fresh
    scratch database instead through `backup.restore_database(path, database=...)`.

    path: the archive `backup` wrote.
    """
    report = await backup_ops.restore_database(path)
    print(f"restored {report.path} into {report.database}")


@app.command
async def serve_mcp() -> None:
    """Run the aizk MCP server, and the background worker beside it, one process, one event loop.

    The one interface through which every memory verb is reached, streamable HTTP when
    AIZK_MCP_HTTP is set and stdio otherwise. When `serve_with_worker` is on (the default), the
    pgqueuer worker that drains the queue and fires every scheduled pass, the auto-backup among
    them, runs gathered on the same loop, so a single container is the whole engine. Set it off to
    run the server alone beside a separate `aizk worker`. fastmcp is imported lazily here so the
    rest of the CLI stays usable without it installed.
    """
    from .mcp.server import server

    logger.info(
        "serving aizk mcp, http={}, worker={}", settings.mcp_http, settings.serve_with_worker
    )
    serving = (
        server.run_http_async(host=settings.mcp_host, port=settings.mcp_port)
        if settings.mcp_http
        else server.run_stdio_async()
    )
    if settings.serve_with_worker:
        await asyncio.gather(serving, run_worker())
    else:
        await serving


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
    pack = await assemble_context_pack(
        query or PROJECT_CONTEXT_QUERY, principal_id=principal or settings.system_principal_id, k=k
    )
    print(
        "\n".join(f"[{block.lane}] {block.line}" for block in pack.blocks) or "no context recalled"
    )


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


@app.command
async def create_admin(zitadel_sub: str, name: str = "admin") -> None:
    """Bind a Zitadel subject to an admin principal and print its id, the identity bootstrap.

    Provisions the principal the machine or human presenting that subject's token administers the
    engine as, minting an admin one stamped with the subject or promoting the one already carrying
    it. The one bootstrap that cannot require auth, since it mints the first identity the token
    verifier resolves an admin to, run once after Zitadel issues the machine account.

    zitadel_sub: the subject claim Zitadel mints this identity's tokens against.
    name: display name for a freshly minted principal.
    """
    async with system_session() as session:
        principal = await Principal.link_admin(session, zitadel_sub, name)
        principal_id = principal.id
    print(principal_id)


if __name__ == "__main__":
    app()
