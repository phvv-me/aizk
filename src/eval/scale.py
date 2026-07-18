import math
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator
from functools import partial
from itertools import batched

import jinja2
import numpy as np
from loguru import logger
from mainboard import meter
from patos import FrozenModel, Model
from pydantic import UUID5, UUID7, UUID8
from sqlalchemy import BigInteger, String, column, func, insert
from sqlalchemy import table as sql_table
from sqlmodel import select

from aizk.config import settings
from aizk.graph.communities import detect
from aizk.ontology import System
from aizk.retrieval import Plan, QueryContext, recall
from aizk.retrieval.recall import build_recall_statement
from aizk.serving.embed import EmbedClient
from aizk.store import (
    Chunk,
    Document,
    Entity,
    Fact,
    TableBase,
)
from aizk.store.engine import Session
from aizk.store.identity import User

from .cleanup import purge_scope
from .metrics import percentile

# A million rows is opt-in because it dominates the default scaling curve.
DEFAULT_SIZES = (1_000, 10_000, 100_000)

# Synthetic corpus shape
CHUNKS_PER_DOC = 10

CHUNKS_PER_ENTITY = 4

PREDICATES = (
    System.Relation.RELATED_TO,
    "depends_on",
    "part_of",
    "cites",
    "supersedes",
)

DEFAULT_REPEATS = 20

INSERT_BATCH = 2_000

_CORPUS_TABLES = (
    "fact_claim",
    "fact_content",
    "community",
    "chunk",
    "entity_claim",
    "entity_content",
    "document",
)

_STATISTICS = sql_table(
    "pg_stat_user_tables",
    column("relid", BigInteger),
    column("relname", String),
)

type Row = dict[
    str,
    UUID5 | UUID7 | UUID8 | str | int | list[float] | list[UUID5] | dict | None,
]


class CorpusScale(FrozenModel):
    """The row counts one corpus size expands to, the shape the generator grows the graph to."""

    chunks: int
    documents: int
    entities: int
    facts: int

    @classmethod
    def for_size(cls, size: int) -> CorpusScale:
        """Derive the corpus shape for a target chunk count."""
        return cls(
            chunks=size,
            documents=math.ceil(size / CHUNKS_PER_DOC),
            entities=max(2, size // CHUNKS_PER_ENTITY),
            facts=size,
        )


class Generated(Model):
    """The running tally of rows already inserted, so each size grows the corpus by the
    delta."""

    documents: int = 0
    chunks: int = 0
    entities: int = 0
    facts: int = 0


class LaneLatency(FrozenModel):
    """The per-call latency percentiles of one timed component at one corpus size."""

    name: str
    p50_ms: float
    p95_ms: float
    p99_ms: float

    @classmethod
    async def timed[T](
        cls, name: str, call: Callable[[], Awaitable[T]], repeats: int
    ) -> LaneLatency:
        """Time an async call over several runs and reduce the wall times to percentiles."""
        samples: list[float] = []
        for _ in range(repeats):
            start = time.perf_counter()
            await call()
            samples.append((time.perf_counter() - start) * 1000.0)
        return cls(
            name=name,
            p50_ms=percentile(samples, 50),
            p95_ms=percentile(samples, 95),
            p99_ms=percentile(samples, 99),
        )


class ScalePoint(FrozenModel):
    """Everything measured at one corpus size, one row of the scaling curve."""

    size: int
    entities: int
    facts: int
    ingest_chunks_per_s: float
    ingest_facts_per_s: float
    recall_p50_ms: float
    recall_p95_ms: float
    recall_p99_ms: float
    lanes: list[LaneLatency]
    multihop_query_ms: float
    community_detect_ms: float
    storage_bytes: int
    index_bytes: int
    peak_host_gb: float
    peak_gpu_gb: float

    def lane_p95(self, name: str) -> float:
        """Read one lane's tail latency, zero when the lane was not measured."""
        return next((lane.p95_ms for lane in self.lanes if lane.name == name), 0.0)


class Budget(FrozenModel):
    """The per-component latency ceilings the scaling curve is flagged against."""

    recall_p95_ms: float = 200.0
    lane_p95_ms: float = 100.0
    multihop_query_ms: float = 100.0
    community_detect_ms: float = 1_000.0


class Knee(FrozenModel):
    """The first corpus size at which one component crossed its budget, where to optimize
    next."""

    component: str
    size: int
    value_ms: float
    budget_ms: float


_TEMPLATE = jinja2.Template(
    """\
{%- if not points %}
scale measured no sizes, no corpus to grow
{%- else -%}
sizes={{ sizes }} budget_recall_p95={{ budget_recall_p95_ms }}ms
{% for point in points %}  {{
    "size={} facts={} ingest={}ch/s recall_p50={}ms p95={}ms p99={}ms multihop={}ms detect={}ms"
    " store={}b index={}b host={}gb".format(
        point.size, point.facts, point.ingest_chunks_per_s, point.recall_p50_ms,
        point.recall_p95_ms,
        point.recall_p99_ms,
        point.multihop_query_ms,
        point.community_detect_ms,
        point.storage_bytes, point.index_bytes, point.peak_host_gb,
    )
}}
{% endfor -%}
{% for point in points %}  lanes {% for lane in point.lanes %}{{
    "{}_p95={}ms".format(lane.name, lane.p95_ms)
}}{{ " " if not loop.last }}{% endfor %} @size={{ point.size }}
{% endfor -%}
knees:
{% if knees %}{% for knee in knees %}  {{
    "knee {} at size={} {}ms over {}ms".format(
        knee.component, knee.size, knee.value_ms, knee.budget_ms,
    )
}}
{% endfor %}{% else %}  no knee, every component stayed within budget
{% endif -%}
{%- endif %}""",
    trim_blocks=True,
    lstrip_blocks=True,
)


class ScaleReport(FrozenModel):
    """The full scaling curve, one row per size and the knee flagged per component."""

    sizes: list[int]
    points: list[ScalePoint]
    budget: Budget
    knees: list[Knee]

    def render(self) -> str:
        """Render this curve as a compact text table, one row per size then the flagged
        knees."""
        points = [
            {
                "size": point.size,
                "facts": point.facts,
                "ingest_chunks_per_s": round(point.ingest_chunks_per_s),
                "recall_p50_ms": round(point.recall_p50_ms, 1),
                "recall_p95_ms": round(point.recall_p95_ms, 1),
                "recall_p99_ms": round(point.recall_p99_ms, 1),
                "multihop_query_ms": round(point.multihop_query_ms, 1),
                "community_detect_ms": round(point.community_detect_ms, 1),
                "storage_bytes": point.storage_bytes,
                "index_bytes": point.index_bytes,
                "peak_host_gb": round(point.peak_host_gb, 2),
                "lanes": [
                    {"name": lane.name, "p95_ms": round(lane.p95_ms, 1)} for lane in point.lanes
                ],
            }
            for point in self.points
        ]
        knees = [
            {
                "component": knee.component,
                "size": knee.size,
                "value_ms": round(knee.value_ms, 1),
                "budget_ms": knee.budget_ms,
            }
            for knee in self.knees
        ]
        return _TEMPLATE.render(
            sizes=self.sizes,
            budget_recall_p95_ms=self.budget.recall_p95_ms,
            points=points,
            knees=knees,
        ).strip()


def unit_vector(rng: np.random.Generator, dim: int) -> list[float]:
    """Draw one L2-normalized random vector at the stored halfvec width, a synthetic
    embedding."""
    vector = rng.standard_normal(dim)
    vector /= np.linalg.norm(vector) or 1.0
    return vector.tolist()


def index_id(user_id: UUID5, kind: str, index: int) -> UUID7:
    """Map a row's kind and index to a stable id, so additive growth never collides on a key.

    The derivation is uuid5 for determinism, reshaped to version 7 so the synthetic rows
    satisfy the same UUID7 id validation production rows do."""
    derived = uuid.uuid5(user_id, f"{kind}-{index}")
    return uuid.UUID(int=(derived.int & ~(0xF << 76) & ~(0x3 << 62)) | (7 << 76) | (0x2 << 62))


def content_id(user_id: UUID5, kind: str, index: int) -> UUID5:
    """Map deterministic synthetic content to the UUID5 used by production content rows."""
    return uuid.uuid5(user_id, f"{kind}-{index}")


def content_hash(user_id: UUID5, index: int) -> UUID8:
    """Map synthetic document content to the UUID8 used by production hashes."""
    derived = uuid.uuid5(user_id, f"content-{index}")
    return uuid.UUID(int=(derived.int & ~(0xF << 76) & ~(0x3 << 62)) | (8 << 76) | (0x2 << 62))


def corpus_batches(
    user: User,
    generated: Generated,
    target: CorpusScale,
    rng: np.random.Generator,
    dim: int,
) -> Iterator[tuple[type[TableBase], list[Row]]]:
    """Stream the additive corpus delta in dependency-ordered insert batches."""
    user_id = user.id
    owned: Row = {"created_by": user_id, "scopes": sorted(user.scopes.write)}
    tables: tuple[tuple[type[TableBase], Iterable[Row]], ...] = (
        (
            Document,
            (
                {
                    "id": index_id(user_id, "document", i),
                    "kind": "note",
                    "title": f"scale document {i}",
                    "content_hash": content_hash(user_id, i),
                    **owned,
                }
                for i in range(generated.documents, target.documents)
            ),
        ),
        (
            Chunk,
            (
                {
                    "id": index_id(user_id, "chunk", i),
                    "document_id": index_id(user_id, "document", i // CHUNKS_PER_DOC),
                    "ord": i % CHUNKS_PER_DOC,
                    "text": (
                        f"scale chunk {i} about entity {i % CHUNKS_PER_ENTITY} and topic {i % 32}"
                    ),
                    "embedding": unit_vector(rng, dim),
                    **owned,
                }
                for i in range(generated.chunks, target.chunks)
            ),
        ),
        (
            Entity.Content,
            (
                {
                    "id": content_id(user_id, "entity", i),
                    "name": f"entity {i}",
                    "type": System.Entity.CONCEPT,
                    "embedding": unit_vector(rng, dim),
                }
                for i in range(generated.entities, target.entities)
            ),
        ),
        (
            Entity.Claim,
            (
                {
                    "id": index_id(user_id, "entity_claim", i),
                    "content_id": content_id(user_id, "entity", i),
                    "attributes": {},
                    **owned,
                }
                for i in range(generated.entities, target.entities)
            ),
        ),
        (
            Fact.Content,
            (
                {
                    "id": content_id(user_id, "fact", i),
                    "subject_id": content_id(user_id, "entity", i % target.entities),
                    "object_id": None
                    if i % 5 == 0
                    else content_id(user_id, "entity", (i * 7 + 1) % target.entities),
                    "predicate": PREDICATES[i % len(PREDICATES)],
                    "statement": (
                        f"entity {i % target.entities} {PREDICATES[i % len(PREDICATES)]}"
                        f" entity {(i * 7 + 1) % target.entities}"
                    ),
                    "embedding": unit_vector(rng, dim),
                }
                for i in range(generated.facts, target.facts)
            ),
        ),
        (
            Fact.Claim,
            (
                {
                    "id": index_id(user_id, "fact_claim", i),
                    "content_id": content_id(user_id, "fact", i),
                    "attributes": {},
                    **owned,
                }
                for i in range(generated.facts, target.facts)
            ),
        ),
    )
    for table, rows in tables:
        for batch in batched(rows, INSERT_BATCH, strict=False):
            yield table, list(batch)


async def insert_rows(
    session: Session, batches: Iterable[tuple[type[TableBase], list[Row]]]
) -> None:
    """Insert dependency-ordered row batches."""
    for table, rows in batches:
        await session.exec(insert(table), params=rows)


def advance_generated(generated: Generated, target: CorpusScale) -> None:
    """Advance the running tally to the target shape just grown to, in place."""
    for field in Generated.model_fields:
        setattr(generated, field, getattr(target, field))


async def grow_corpus(
    user: User,
    generated: Generated,
    target: CorpusScale,
    rng: np.random.Generator,
) -> float:
    """Grow the throwaway corpus to a target size and return the ingestion throughput in
    chunks/s."""
    new_chunks = target.chunks - generated.chunks
    start = time.perf_counter()
    async with user as session:
        await insert_rows(
            session, corpus_batches(user, generated, target, rng, settings.embed_dim)
        )
    elapsed = time.perf_counter() - start
    advance_generated(generated, target)
    rate = new_chunks / elapsed if elapsed else 0.0
    logger.info(
        "grew corpus to {size} chunks in {elapsed:.2f}s, {rate:.0f} chunks/s",
        size=target.chunks,
        elapsed=elapsed,
        rate=rate,
    )
    return rate


async def measure_lanes(
    session: Session,
    query: str,
    vector: list[float],
    k: int,
    repeats: int,
) -> list[LaneLatency]:
    """Time each composed SQL plan shape on one shared session."""

    async def retrieve(plan: Plan) -> None:
        context = QueryContext(dimensions=len(vector), fuzzy=settings.graph_mention_fuzzy)
        statement = build_recall_statement(context, plan)
        await session.exec(
            statement,
            params={
                "qvec": vector,
                "qtext": query,
                "qentities": [],
                "k": k,
                **settings.for_statement(statement),
            },
        )

    async def scoped_count() -> None:
        await session.exec(select(Chunk.id.count()))

    async def local() -> None:
        await retrieve(Plan.focused())

    async def global_() -> None:
        await retrieve(Plan.overview())

    async def multihop() -> None:
        await retrieve(Plan.multihop())

    async def maximal() -> None:
        await retrieve(Plan.maximal())

    lanes: dict[str, Callable[[], Awaitable[None]]] = {
        "local": local,
        "global": global_,
        "multihop": multihop,
        "maximal": maximal,
        "rls": scoped_count,
    }
    return [await LaneLatency.timed(name, call, repeats) for name, call in lanes.items()]


async def measure_community_detection(session: Session) -> float:
    """Time batch community detection over the visible graph."""
    facts = list(await session.exec(Fact.Live.embedded()))
    start = time.perf_counter()
    detect(facts, settings.community_min_size)
    return (time.perf_counter() - start) * 1000.0


async def storage_footprint(session: Session) -> tuple[int, int]:
    """Return total and index bytes occupied by the benchmark corpus tables."""
    footprint = (
        await session.exec(
            select(
                func.coalesce(func.sum(func.pg_total_relation_size(_STATISTICS.c.relid)), 0),
                func.coalesce(func.sum(func.pg_indexes_size(_STATISTICS.c.relid)), 0),
            )
            .select_from(_STATISTICS)
            .where(_STATISTICS.c.relname.in_(_CORPUS_TABLES))
        )
    ).one()
    return int(footprint[0]), int(footprint[1])


async def measure_point(
    user: User,
    query: str,
    vector: list[float],
    scale: CorpusScale,
    ingest_chunks_per_s: float,
    baseline_storage: tuple[int, int],
    k: int,
    repeats: int,
) -> ScalePoint:
    """Measure one corpus size into a curve row, recall, per-lane, graph-op, and storage."""
    with meter() as runtime:
        recalled = await LaneLatency.timed("recall", partial(recall, query, user, k), repeats)
        runtime.sample()
        async with user as session:
            lanes = await measure_lanes(session, query, vector, k, repeats)
            multihop_ms = next(lane.p50_ms for lane in lanes if lane.name == "multihop")
            detect_ms = await measure_community_detection(session)
            footprint = await storage_footprint(session)
            runtime.sample()
    return ScalePoint(
        size=scale.chunks,
        entities=scale.entities,
        facts=scale.facts,
        ingest_chunks_per_s=ingest_chunks_per_s,
        ingest_facts_per_s=ingest_chunks_per_s,
        recall_p50_ms=recalled.p50_ms,
        recall_p95_ms=recalled.p95_ms,
        recall_p99_ms=recalled.p99_ms,
        lanes=lanes,
        multihop_query_ms=multihop_ms,
        community_detect_ms=detect_ms,
        storage_bytes=max(0, footprint[0] - baseline_storage[0]),
        index_bytes=max(0, footprint[1] - baseline_storage[1]),
        peak_host_gb=runtime.peak_host_gb,
        peak_gpu_gb=runtime.peak_gpu_gb,
    )


def find_knees(points: list[ScalePoint], budget: Budget) -> list[Knee]:
    """Flag the first size each tracked component crossed its budget at, the optimization
    targets."""
    readers: list[tuple[str, float, Callable[[ScalePoint], float]]] = [
        ("recall_p95", budget.recall_p95_ms, lambda point: point.recall_p95_ms),
        (
            "multihop_query",
            budget.multihop_query_ms,
            lambda point: point.multihop_query_ms,
        ),
        ("community_detect", budget.community_detect_ms, lambda point: point.community_detect_ms),
    ]
    lane_names = sorted({lane.name for point in points for lane in point.lanes})
    readers += [
        (f"lane:{name}", budget.lane_p95_ms, partial(ScalePoint.lane_p95, name=name))
        for name in lane_names
    ]
    return [
        Knee(component=component, size=breach.size, value_ms=read(breach), budget_ms=limit)
        for component, limit, read in readers
        if (breach := next((point for point in points if read(point) > limit), None)) is not None
    ]


async def measure_size(
    user: User,
    query: str,
    vector: list[float],
    size: int,
    generated: Generated,
    rng: np.random.Generator,
    baseline_storage: tuple[int, int],
    k: int,
    repeats: int,
) -> ScalePoint:
    """Grow the corpus to one size and measure it, logging the curve row for this size."""
    scale = CorpusScale.for_size(size)
    ingest_rate = await grow_corpus(user, generated, scale, rng)
    point = await measure_point(
        user, query, vector, scale, ingest_rate, baseline_storage, k, repeats
    )
    logger.info(
        "scale size={size} recall_p95={p95:.1f}ms multihop={multihop:.1f}ms detect={det:.1f}ms",
        size=size,
        p95=point.recall_p95_ms,
        multihop=point.multihop_query_ms,
        det=point.community_detect_ms,
    )
    return point


async def run_scale_benchmark(
    sizes: tuple[int, ...] = DEFAULT_SIZES,
    k: int = 8,
    repeats: int = DEFAULT_REPEATS,
    query: str = "what relates the central entities of the graph",
    budget: Budget | None = None,
    seed: int = 0,
    keep: bool = False,
) -> ScaleReport:
    """Grow a throwaway corpus through the sizes and measure the scaling curve, flagging each
    knee."""
    budget = budget or Budget()
    rng = np.random.default_rng(seed)
    [vector] = await EmbedClient.from_settings(settings).embed([query], mode="query")
    # A fresh personal scope isolates each benchmark corpus.
    user = User.private(uuid.uuid5(uuid.NAMESPACE_URL, f"aizk-scale:{uuid.uuid7()}"))
    generated = Generated()
    points: list[ScalePoint] = []
    async with user as session:
        baseline_storage = await storage_footprint(session)
    try:
        for size in sorted(sizes):
            points.append(
                await measure_size(
                    user,
                    query,
                    vector,
                    size,
                    generated,
                    rng,
                    baseline_storage,
                    k,
                    repeats,
                )
            )
    finally:
        if not keep:
            await purge_scope(user.scopes.write)
    return ScaleReport(
        sizes=sorted(sizes), points=points, budget=budget, knees=find_knees(points, budget)
    )
