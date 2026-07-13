import uuid
from collections.abc import Sequence
from pathlib import Path

from mainboard.profiling import SpanStat, default_collector
from patos import FrozenModel
from sqlalchemy import func
from sqlmodel import select

from . import export, graph, ops
from .background.queue import enqueue_pending
from .background.status import TasksStatus, tasks_overview
from .config import settings
from .eval import (
    BenchmarkReport,
    BenchmarkRunner,
    Budget,
    EvalReport,
    GateReport,
    GroupMemBench,
    PlanStudyReport,
    QuestionKind,
    Stratum,
    measure_gate,
    run_eval,
    run_plan_study,
    run_sweep,
)
from .eval.scale import ScaleReport, run_scale_benchmark
from .eval.sweep import SweepReport
from .extract import ingest as extract_ingest
from .extract import ontology
from .serving import embed
from .store import (
    Chunk,
    Document,
    EntityContent,
    EntityKind,
    FactClaim,
    FactContent,
    RelationKind,
    as_system,
)
from .store.identity import User


class ForgetResult(FrozenModel):
    """What one forget retracted, the erasure counterpart to a write."""

    documents: list[str]
    claims: int


class OntologyKindRow(FrozenModel):
    """One ontology kind with how much of the graph uses it, the catalog review row."""

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
    """Build the graph now over the user's pending chunks, the on-demand extraction."""
    return await graph.build_graph(
        limit=limit, scopes=frozenset({user_id or system()}), source=source
    )


async def decay(half_life_days: float = 90.0, user_id: uuid.UUID | None = None) -> int:
    """Run the decay pass now, archiving stale facts that leave recall but stay in history."""
    return await graph.decay(
        scopes=frozenset({user_id or system()}), half_life_days=half_life_days
    )


async def reembed(user_id: uuid.UUID | None = None) -> int:
    """Re-embed every visible stored vector with the current embedder, a backend migration."""
    return await graph.reembed(scopes=frozenset({user_id or system()}))


async def raptor(user_id: uuid.UUID | None = None) -> int:
    """Build the RAPTOR tree now, the recursive summary tiers above the communities."""
    return await graph.build_raptor(scopes=frozenset({user_id or system()}))


async def forget(query: str, k: int = 8, user_id: uuid.UUID | None = None) -> ForgetResult:
    """Retract the claims a query's own source notes contributed, remember's erasure
    counterpart."""
    actor = user_id or system()
    [vector] = await embed([query], mode="query")
    async with User.system({actor}) as session:
        distance = Chunk.embedding @ vector
        doc_ids = list(
            await session.exec(
                select(Chunk.document_id)
                .where(Chunk.embedding.is_not(None))
                .group_by(Chunk.document_id)
                .order_by(func.min(distance))
                .limit(k)
            )
        )
        titles = list(await session.exec(select(Document.title).where(Document.id.in_(doc_ids))))
        retracted = await FactClaim.forget_from_documents(session, doc_ids)
    return ForgetResult(documents=[t for t in titles if t], claims=len(retracted))


async def promote(document: str, to_scopes: str, user_id: uuid.UUID | None = None) -> int:
    """Promote a document and its chunks and facts into a wider scope-set as a new audited
    copy."""
    actor = user_id or system()
    target = settings.scope_ids(to_scopes)
    authority = frozenset((actor, *target))
    user = User.authorized(actor, read=authority, write=authority)
    return await graph.promote([uuid.UUID(document)], target, user)


async def ingest(path: str, scopes: str | None = None, user_id: uuid.UUID | None = None) -> int:
    """Ingest a file or directory of notes and code into memory, the document count back."""
    actor = user_id or system()
    target = settings.scope_ids(scopes) or frozenset({actor})
    ingested = await extract_ingest.ingest_path(
        User.system(target), Path(path), created_by=actor, scopes=target
    )
    await enqueue_pending(scopes=target)
    return ingested


async def ingest_image(
    path: str,
    caption: str | None = None,
    scopes: str | None = None,
    user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Ingest an image into the shared multimodal space so a text query can recall it."""
    actor = user_id or system()
    target = settings.scope_ids(scopes) or frozenset({actor})
    document_id = await extract_ingest.ingest_image(
        User.system(target), Path(path), caption=caption, created_by=actor, scopes=target
    )
    await enqueue_pending(scopes=target)
    return document_id


async def export_scope(path: str, user_id: uuid.UUID | None = None) -> export.ExportReport:
    """Export a user's visible memory to a JSONL file, the scoped portable dump."""
    return await export.export_scope(Path(path), user=User.system({user_id or system()}))


async def audit(limit: int = 20, user_id: uuid.UUID | None = None) -> list[Document]:
    """The most recent visible document writes, for the operator's write log."""
    actor = user_id or system()
    async with User.system({actor}) as session:
        return list(
            await session.exec(select(Document).order_by(Document.created_at.desc()).limit(limit))
        )


async def define_entity_kind(name: str, description: str, domain: str = "general") -> None:
    """Add or refine an entity type in the live ontology, refreshing the extraction snapshot."""
    async with as_system() as session:
        await EntityKind.define(session, name, description, domain)
        await ontology.refresh(session)


async def define_relation_kind(name: str, description: str, domain: str = "general") -> None:
    """Add or refine a relation predicate in the live ontology, refreshing the extraction
    snapshot."""
    async with as_system() as session:
        await RelationKind.define(session, name, description, domain)
        await ontology.refresh(session)


async def list_ontology() -> list[OntologyKindRow]:
    """Every ontology kind with how much of the graph uses it, the catalog review surface."""
    async with as_system() as session:
        entity_uses = dict(
            (
                await session.exec(
                    select(EntityContent.type, func.count()).group_by(EntityContent.type)
                )
            ).all()
        )
        relation_uses = dict(
            (
                await session.exec(
                    select(FactContent.predicate, func.count()).group_by(FactContent.predicate)
                )
            ).all()
        )
        entity_kinds = list(await session.exec(select(EntityKind).order_by(EntityKind.name)))
        relation_kinds = list(await session.exec(select(RelationKind).order_by(RelationKind.name)))
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
    """Run the eval harness over visible memory and report hit-at-k with a per-config split."""
    questions = _read_questions(questions_file)
    return await run_eval(questions, k=k, user=User.system())


async def sweep(questions_file: str | None = None, k: int = 8) -> SweepReport:
    """Sweep the config grid and report quality, latency, and memory for each config."""
    questions = _read_questions(questions_file)
    return await run_sweep(questions, k=k, user=User.system())


async def plan_study(
    k: int = 8,
    per_stratum: int = 8,
    strata: Sequence[str] = tuple(stratum.value for stratum in Stratum),
    seeding: bool = True,
    gate_limit: int | None = None,
) -> PlanStudyReport:
    """Run the stratified plan study, optionally replaying the build gate into the report."""
    report = await run_plan_study(
        user=User.system(),
        k=k,
        per_stratum=per_stratum,
        strata=tuple(Stratum(stratum) for stratum in strata),
        seeding=seeding,
    )
    if gate_limit is None:
        return report
    return report.model_copy(update={"gate": await measure_gate(limit=gate_limit)})


async def gate_check(limit: int | None = 50, user_id: uuid.UUID | None = None) -> GateReport:
    """Replay the build gate over stored chunks and force-extract the rejected ones."""
    return await measure_gate(scopes=frozenset({user_id or system()}), limit=limit)


async def groupmem(
    root: str,
    domain: str = "Finance",
    kinds: Sequence[str] = tuple(kind.value for kind in QuestionKind),
    message_limit: int | None = None,
    question_limit: int | None = None,
    k: int = 10,
    prepare: bool = True,
    keep: bool = False,
) -> BenchmarkReport:
    """Run GroupMemBench through authored ingestion, graph build, recall, answer, and judge."""
    dataset = GroupMemBench(root=Path(root)).load(
        domain,
        kinds=tuple(QuestionKind(kind) for kind in kinds),
        message_limit=message_limit,
        question_limit=question_limit,
    )
    return await BenchmarkRunner.configured(k=k).run(dataset, prepare=prepare, keep=keep)


async def scale(
    sizes: Sequence[int] = (1000, 10000),
    k: int = 8,
    repeats: int = 10,
    recall_p95_ms: float = 200.0,
) -> ScaleReport:
    """Grow a throwaway corpus through the sizes and report the scaling curve with each knee."""
    return await run_scale_benchmark(
        sizes=tuple(sizes), k=k, repeats=repeats, budget=Budget(recall_p95_ms=recall_p95_ms)
    )


async def setup() -> ops.SetupReport:
    """Bring the database to a ready state, migrating to head and installing the queue
    schema."""
    return await ops.setup()


async def health() -> ops.HealthReport:
    """The engine's schema, row security, row-count, queue, and serving-endpoint state."""
    return await ops.health()


def _read_questions(questions_file: str | None) -> list[str] | None:
    """The lines of a questions file, or null to let the eval synthesize its own from facts."""
    if not questions_file:
        return None
    return Path(questions_file).read_text(encoding="utf-8").splitlines()
