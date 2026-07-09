"""The operator plane: every maintenance, governance, and eval operation, CLI-only by design.

These are the operations an operator runs by ssh-ing to the box and driving the `aizk` CLI, the
half of the surface that never belongs on the network-reachable MCP server. Each is a plain async
function over the existing `graph`/`ops`/`export`/`eval`/`store` layers, returning a scalar, a
store row, or a small local result, so the CLI stays thin presentation and the logic is tested
here directly rather than through a tool wrapper. The MCP server keeps only the client verbs
(recall, remember, and reference), and everything in this module is reached through the CLI, so a
leaked API key can never drive a rebuild, a promotion, or a grant.

Operator functions act as the system user by default (the owner role, past row level
security), the identity an ssh operator legitimately is, and take an explicit `user_id`
where the operation is genuinely one tenant's view (forget, export, audit).
"""

import uuid
from collections.abc import Sequence
from pathlib import Path

from mainboard.profiling import SpanStat, default_collector
from patos import FrozenModel
from sqlalchemy import func, select

from . import export, graph, ops
from .background.status import TasksStatus, tasks_overview
from .config import settings
from .eval import Budget, EvalReport, SweepMatrix, benchmarks, run_eval, run_sweep
from .eval.scale import ScaleReport, run_scale_benchmark
from .eval.sweep import SweepReport
from .extract import ingest as extract_ingest
from .extract import ontology
from .scopes import scopes_from_org_ids
from .serving import Embedder
from .store import (
    Chunk,
    Document,
    EntityContent,
    EntityKind,
    FactClaim,
    FactContent,
    RelationKind,
    acting_as,
    as_system,
)
from .store.engine import caller_standing, session


class ForgetResult(FrozenModel):
    """What one forget retracted, the erasure counterpart to a write.

    documents: titles of the source notes whose derived claims were retracted, so the operator
        sees exactly what was forgotten before committing to the reversible retraction.
    claims: how many live claims left the live graph, closed in `recorded` but kept in history.
    """

    documents: list[str]
    claims: int


class OntologyKindRow(FrozenModel):
    """One ontology kind with how much of the graph uses it, the catalog review row.

    name: the vocabulary member, the type or predicate a content row stores.
    kind: entity for an entity type, relation for a fact predicate.
    description: the one-line gloss the extraction prompt renders.
    domain: the grouping tag, core, general, coding, research, finance, personal, or auto.
    structural: whether the system writes this one and the extractor never emits it.
    uses: how many live content rows currently carry this type or predicate.
    """

    name: str
    kind: str
    description: str
    domain: str
    structural: bool
    uses: int


def system() -> uuid.UUID:
    """The system user id, the identity an operator's CLI call acts as by default."""
    return settings.system_user_id


async def rebuild(
    limit: int | None = None, source: str | None = None, user_id: uuid.UUID | None = None
) -> tuple[int, int]:
    """Build the graph now over the user's pending chunks, the on-demand extraction.

    Runs inline rather than waiting for the worker to drain the queue, returning the entities and
    facts created. The autonomous default is the queue the worker drains.

    limit: maximum number of chunks to process, all of them when null.
    source: restrict the build to chunks of documents whose title matches this substring.
    user_id: identity that owns the written claims, the system user when null.
    """
    return await graph.build_graph(limit=limit, user_id=user_id or system(), source=source)


async def decay(half_life_days: float = 90.0, user_id: uuid.UUID | None = None) -> int:
    """Run the decay pass now, archiving stale facts that leave recall but stay in history.

    half_life_days: age in days at which an unaccessed fact's relevance halves.
    user_id: identity whose facts are decayed, the system user when null.
    """
    return await graph.decay(user_id=user_id or system(), half_life_days=half_life_days)


async def reembed(user_id: uuid.UUID | None = None) -> int:
    """Re-embed every visible stored vector with the current embedder, a backend migration.

    Re-encodes the chunk, entity, fact, community, and profile embeddings from their stored source
    text, so switching the embed model needs no re-ingest.

    user_id: identity whose vectors are re-embedded, the system user when null.
    """
    return await graph.reembed(user_id=user_id or system())


async def raptor(user_id: uuid.UUID | None = None) -> int:
    """Build the RAPTOR tree now, the recursive summary tiers above the communities.

    Clusters the communities up level by level into the summary-of-summaries a broad query reads.
    Build the communities first, since the tree climbs above them.

    user_id: identity whose tree is built, the system user when null.
    """
    return await graph.build_raptor(user_id=user_id or system())


async def forget(query: str, k: int = 8, user_id: uuid.UUID | None = None) -> ForgetResult:
    """Retract the claims a query's own source notes contributed, remember's erasure counterpart.

    Where decay forgets by age, this forgets by provenance: it finds the notes most relevant to the
    query and closes the recorded range on every live claim derived from them, so knowledge that
    should never have been mined leaves live recall while the notes themselves stay indexable.
    Reversible, nothing is deleted, so describe what to forget the way you would recall it and
    start narrow.

    query: what to forget, described the way you would recall it.
    k: how many of the most relevant source notes to retract the derived claims of.
    user_id: identity whose notes are searched and retracted, the system user when null.
    """
    owner = user_id or system()
    [vector] = await Embedder().embed([query], mode="query")
    async with acting_as(owner):
        ranked = (
            await session().execute(
                select(Chunk.document_id)
                .order_by(Chunk.embedding.cosine_distance(vector))
                .limit(k * 4)
            )
        ).scalars()
        doc_ids = list(dict.fromkeys(ranked))[:k]
        titles = list(
            (
                await session().execute(select(Document.title).where(Document.id.in_(doc_ids)))
            ).scalars()
        )
        retracted = await FactClaim.forget_from_documents(doc_ids)
    return ForgetResult(documents=[t for t in titles if t], claims=len(retracted))


async def promote(document: str, to_scopes: str, user_id: uuid.UUID | None = None) -> int:
    """Promote a document and its chunks and facts into a wider scope-set as a new audited copy.

    A deliberate governance write, never autonomous, so widening a memory's visibility always
    passes through the operator.

    document: id of the source document to promote.
    to_scopes: comma-separated names of the target groups the copy is published into.
    user_id: identity the promotion acts under, the system user when null.
    """
    return await graph.promote(uuid.UUID(document), to_scopes, user_id=user_id or system())


async def ingest(path: str, scopes: str | None = None, user_id: uuid.UUID | None = None) -> int:
    """Ingest a file or directory of notes and code into memory, the document count back.

    Code files are chunked AST-aware and stamped `kind=code`, notes flow through the prose
    splitter, and a file whose content hash already exists is skipped.

    path: file or directory to ingest.
    scopes: comma-separated group names to share it with, private to the owner when null.
    user_id: identity that owns the stored rows, the system user when null.
    """
    owner = user_id or system()
    target = scopes_from_org_ids(scopes)
    with caller_standing(target, target):
        return await extract_ingest.ingest_path(Path(path), owner_id=owner, scopes=target)


async def ingest_image(
    path: str,
    caption: str | None = None,
    scopes: str | None = None,
    user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Ingest an image into the shared multimodal space so a text query can recall it.

    The image embeds through the served model's image lane into the same space the text chunks
    live in, so the embed endpoint must serve a multimodal model or the call fails fast.

    path: image file to ingest.
    caption: text stored on the chunk and shown in recall, the file name when null.
    scopes: comma-separated group names to share it with, private to the owner when null.
    user_id: identity that owns the stored row, the system user when null.
    """
    owner = user_id or system()
    target = scopes_from_org_ids(scopes)
    with caller_standing(target, target):
        return await extract_ingest.ingest_image(
            Path(path), caption=caption, owner_id=owner, scopes=target
        )


async def export_scope(path: str, user_id: uuid.UUID | None = None) -> export.ExportReport:
    """Export a user's visible memory to a JSONL file, the scoped portable dump.

    Writes every document, chunk, entity, and fact the user can see, the facts carrying both
    their valid-time and transaction-time windows so the bi-temporal history rides along. Runs
    under that user's own row level security, so exactly the rows it may see leave.

    path: the JSONL file the dump is written to.
    user_id: identity whose visible rows are exported, the system user when null.
    """
    return await export.export_scope(Path(path), user_id=user_id or system())


async def audit(limit: int = 20, user_id: uuid.UUID | None = None) -> list[Document]:
    """The most recent visible document writes, for the operator's write log.

    limit: maximum number of writes to return.
    user_id: identity whose visible writes are listed, the system user when null.
    """
    async with acting_as(user_id or system()):
        return list(
            await session().scalars(
                select(Document).order_by(Document.created_at.desc()).limit(limit)
            )
        )


# There is no user, org, or membership operator surface: identity and org standing live entirely
# in Logto now, so a user is onboarded, an org created, a member added, and an org published
# through Logto's own admin rather than an `aizk` verb. aizk derives every id from the verified
# token, so nothing here mirrors that state. The remaining operator surface is graph maintenance,
# ontology, ingest, eval, and database ops, none of which touch identity.


async def define_entity_kind(name: str, description: str, domain: str = "general") -> None:
    """Add or refine an entity type in the live ontology, refreshing the extraction snapshot.

    Writes the type into the catalog so the very next extraction may emit it; a repeat over a
    present name just sharpens its gloss. Grow-only, so this is always add or refine, never delete.

    name: the type a content row stores, a noun in PascalCase such as Area or Milestone.
    description: one-line gloss the extraction prompt renders and the auto-create fold matches.
    domain: grouping tag, general by default, or core, coding, research, finance, personal.
    """
    async with as_system():
        await EntityKind.define(name, description, domain)
        await ontology.refresh()


async def define_relation_kind(name: str, description: str, domain: str = "general") -> None:
    """Add or refine a relation predicate in the live ontology, refreshing the extraction snapshot.

    name: the predicate a fact stores, a snake_case verb phrase such as part_of or funds.
    description: one-line gloss the extraction prompt renders and the auto-create fold matches.
    domain: grouping tag, general by default, or core, coding, research, finance, personal.
    """
    async with as_system():
        await RelationKind.define(name, description, domain)
        await ontology.refresh()


async def list_ontology() -> list[OntologyKindRow]:
    """Every ontology kind with how much of the graph uses it, the catalog review surface.

    Entity types first, then relation predicates, each with the count of live content rows that
    carry it, so the operator sees the whole vocabulary at once and can tell a load-bearing type
    from dead weight worth folding into another.
    """
    async with as_system():
        entity_uses = dict(
            (
                await session().execute(
                    select(EntityContent.type, func.count()).group_by(EntityContent.type)
                )
            ).all()
        )
        relation_uses = dict(
            (
                await session().execute(
                    select(FactContent.predicate, func.count()).group_by(FactContent.predicate)
                )
            ).all()
        )
        entity_kinds = list(await session().scalars(select(EntityKind).order_by(EntityKind.name)))
        relation_kinds = list(
            await session().scalars(select(RelationKind).order_by(RelationKind.name))
        )
    return [
        OntologyKindRow(
            name=kind.name,
            kind=label,
            description=kind.description,
            domain=kind.domain,
            structural=kind.structural,
            uses=uses.get(kind.name, 0),
        )
        for label, kinds, uses in (
            ("entity", entity_kinds, entity_uses),
            ("relation", relation_kinds, relation_uses),
        )
        for kind in kinds
    ]


async def tasks_status() -> TasksStatus:
    """The autonomous engine's pending, running, failed, last-run, and lag counts."""
    return await tasks_overview()


def profile_report() -> list[SpanStat]:
    """The process-wide span timing stats mainboard.profiling collected, slowest first."""
    return default_collector().stats()


async def bench(questions_file: str | None = None, k: int = 8) -> EvalReport:
    """Run the eval harness over visible memory and report hit-at-k with a per-config split.

    questions_file: a file of one question per line, or null to synthesize them from facts.
    k: how many hits and seed facts each recall surfaces.
    """
    questions = _read_questions(questions_file)
    return await run_eval(questions, k=k, user_id=system())


async def sweep(
    questions_file: str | None = None, k: int = 8, dims: str | None = None
) -> SweepReport:
    """Sweep the config grid and report quality, latency, and memory for each config.

    questions_file: a file of one question per line, or null to synthesize them from facts.
    k: how many hits and seed facts each recall surfaces.
    dims: comma-separated Matryoshka widths to sweep, the live width when null.
    """
    questions = _read_questions(questions_file)
    matrix = SweepMatrix(embed_dim=[int(dim) for dim in dims.split(",")] if dims else [])
    return await run_sweep(questions, k=k, user_id=system(), matrix=matrix)


async def benchmark(name: str, dataset_path: str, k: int = 8) -> SweepReport:
    """Sweep the config grid over one external 2026 benchmark loaded from its dataset file.

    Gated by `benchmarks_enabled` since the datasets are an optional dev download.

    name: which benchmark to load, `evermembench` or `tempo`.
    dataset_path: path to the benchmark's JSONL file.
    k: how many hits and seed facts each recall surfaces.
    """
    if not settings.benchmarks_enabled:
        raise ValueError("aizk benchmarks are off, set AIZK_BENCHMARKS_ENABLED to run them")
    if name not in benchmarks.LOADERS:
        raise ValueError(
            f"unknown benchmark {name!r}, expected one of {sorted(benchmarks.LOADERS)}"
        )
    gold = benchmarks.benchmark_gold(benchmarks.LOADERS[name](Path(dataset_path)))
    return await run_sweep(None, k=k, user_id=system(), gold=gold)


async def scale(
    sizes: Sequence[int] = (1000, 10000),
    k: int = 8,
    repeats: int = 10,
    recall_p95_ms: float = 200.0,
) -> ScaleReport:
    """Grow a throwaway corpus through the sizes and report the scaling curve with each knee.

    sizes: corpus chunk counts to measure, the hundred-thousand point left opt-in.
    k: how many hits and seed facts each recall surfaces.
    repeats: how many recall and per-lane calls each percentile is read over.
    recall_p95_ms: the tail recall budget in milliseconds the recall knee is flagged against.
    """
    return await run_scale_benchmark(
        sizes=tuple(sizes), k=k, repeats=repeats, budget=Budget(recall_p95_ms=recall_p95_ms)
    )


async def setup() -> ops.SetupReport:
    """Bring the database to a ready state, migrating to head and installing the queue schema."""
    return await ops.setup()


async def health() -> ops.HealthReport:
    """The engine's schema, row security, row-count, queue, and serving-endpoint state."""
    return await ops.health()


def _read_questions(questions_file: str | None) -> list[str] | None:
    """The lines of a questions file, or null to let the eval synthesize its own from facts."""
    if not questions_file:
        return None
    return Path(questions_file).read_text(encoding="utf-8").splitlines()
