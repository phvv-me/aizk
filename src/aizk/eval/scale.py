import math
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import partial

import jinja2
import numpy as np
from loguru import logger
from patos import FrozenModel, Model
from sqlalchemy import delete, func, insert, select, text

from ..config import settings
from ..extract import ontology
from ..graph.algos import ppr_expand
from ..graph.communities import detect
from ..retrieval import Recall, recall
from ..serving import Embedder
from ..store import (
    Chunk,
    Community,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    LiveFact,
    Profile,
    TableBase,
    User,
    Watermark,
    acting_as,
    as_system,
)
from ..store.engine import session
from .sweep import open_meter, percentile

# corpus sizes the scaling curve is read at; a million is opt-in since one run writes a million
# embedded rows, a cost the default sweep should not pay each time.
DEFAULT_SIZES = (1_000, 10_000, 100_000)

# chunks packed under one document, so the chunk-to-document join sees a realistic shape.
CHUNKS_PER_DOC = 10

# entities are a quarter of the chunk count, a graph dense enough for pagerank and detection.
CHUNKS_PER_ENTITY = 4

# the closed predicate vocabulary the synthetic facts cycle through.
PREDICATES = (
    ontology.RELATED_TO,
    ontology.DEPENDS_ON,
    ontology.PART_OF,
    ontology.CITES,
    ontology.SUPERSEDES,
)

# recall and per-lane calls timed per size, wide enough that a p99 is not a single sample.
DEFAULT_REPEATS = 20

# rows pushed per executemany so a 100k-row generation streams in bounded batches.
INSERT_BATCH = 2_000

# the tables the corpus lands in, read for footprint and emptied in dependency order on purge.
CORPUS_TABLES = (
    "fact_claim",
    "fact_content",
    "community",
    "chunk",
    "entity_claim",
    "entity_content",
    "document",
)

type Row = dict[str, uuid.UUID | str | int | list[float] | dict | None]


class CorpusScale(FrozenModel):
    """The row counts one corpus size expands to, the shape the generator grows the graph to.

    chunks: embedded text spans, the size the curve is indexed by.
    documents: parent documents the chunks pack under.
    entities: graph nodes the facts connect.
    facts: bi-temporal graph edges, one per chunk.
    """

    chunks: int
    documents: int
    entities: int
    facts: int

    @classmethod
    def for_size(cls, size: int) -> CorpusScale:
        """Derive the corpus shape for a target chunk count.

        size: number of embedded chunks the corpus holds at this point.
        """
        return cls(
            chunks=size,
            documents=math.ceil(size / CHUNKS_PER_DOC),
            entities=max(2, size // CHUNKS_PER_ENTITY),
            facts=size,
        )


class Generated(Model):
    """The running tally of rows already inserted, so each size grows the corpus by the delta.

    Holding the counts keeps generation purely additive, every index inserted exactly once, so
    the entity a fact points at always exists and a re-grown corpus never collides on a key.

    documents: documents inserted so far.
    chunks: chunks inserted so far.
    entities: entities inserted so far.
    facts: facts inserted so far.
    """

    documents: int = 0
    chunks: int = 0
    entities: int = 0
    facts: int = 0


class LaneLatency(FrozenModel):
    """The per-call latency percentiles of one timed component at one corpus size.

    name: the component the percentiles describe, such as recall or a retrieval lane.
    p50_ms: median per-call wall time in milliseconds.
    p95_ms: tail per-call wall time in milliseconds.
    p99_ms: far-tail per-call wall time in milliseconds.
    """

    name: str
    p50_ms: float
    p95_ms: float
    p99_ms: float

    @classmethod
    async def timed(
        cls, name: str, call: Callable[[], Awaitable[object]], repeats: int
    ) -> LaneLatency:
        """Time an async call over several runs and reduce the wall times to percentiles.

        name: the component the percentiles describe.
        call: the zero-argument coroutine factory to time.
        repeats: how many runs the percentiles are read over.
        """
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
    """Everything measured at one corpus size, one row of the scaling curve.

    size: the chunk count the corpus held when measured.
    entities: graph nodes present at this size.
    facts: graph edges present at this size.
    ingest_chunks_per_s: chunks written per second growing the corpus to this size.
    ingest_facts_per_s: facts written per second over the same growth.
    recall_p50_ms: median end-to-end recall wall time at this size.
    recall_p95_ms: tail end-to-end recall wall time, the budget the knee is read against.
    recall_p99_ms: far-tail end-to-end recall wall time.
    lanes: the per-lane latency breakdown, vector through the row-level-security predicate.
    ppr_query_ms: median personalized-pagerank expand time per query, the networkx graph op.
    community_detect_ms: greedy-modularity community detection time over the graph, the batch op.
    storage_bytes: total on-disk footprint of the corpus tables, heap plus indexes plus toast.
    index_bytes: index-only footprint, the pgvector and lexical structures whose RAM grows fastest.
    peak_host_gb: highest host memory in use across the measurements at this size.
    peak_gpu_gb: highest total GPU memory in use across the measurements at this size.
    """

    size: int
    entities: int
    facts: int
    ingest_chunks_per_s: float
    ingest_facts_per_s: float
    recall_p50_ms: float
    recall_p95_ms: float
    recall_p99_ms: float
    lanes: list[LaneLatency]
    ppr_query_ms: float
    community_detect_ms: float
    storage_bytes: int
    index_bytes: int
    peak_host_gb: float
    peak_gpu_gb: float

    def lane_p95(self, name: str) -> float:
        """Read one lane's tail latency, zero when the lane was not measured.

        name: the lane whose p95 to read.
        """
        return next((lane.p95_ms for lane in self.lanes if lane.name == name), 0.0)


class Budget(FrozenModel):
    """The per-component latency ceilings the scaling curve is flagged against.

    recall_p95_ms: largest acceptable tail recall latency before the recall path is the knee.
    lane_p95_ms: largest acceptable tail latency for any single retrieval lane.
    ppr_query_ms: largest acceptable pagerank expand time, the networkx-versus-CTE line.
    community_detect_ms: largest acceptable detection time, the networkx-versus-cuGraph line.
    """

    recall_p95_ms: float = 200.0
    lane_p95_ms: float = 100.0
    ppr_query_ms: float = 100.0
    community_detect_ms: float = 1_000.0


class Knee(FrozenModel):
    """The first corpus size at which one component crossed its budget, where to optimize next.

    component: the measured component that broke its budget, such as recall_p95 or ppr_query.
    size: the smallest corpus size at which the component exceeded its ceiling.
    value_ms: the component's measured latency at that size.
    budget_ms: the ceiling the value crossed.
    """

    component: str
    size: int
    value_ms: float
    budget_ms: float


# renders a scaling curve as a compact text table, one row per size, the per-lane latency
# breakdown, then the flagged knees, the numbers already rounded so the template stays structural.
_TEMPLATE = jinja2.Template(
    """\
{%- if not points %}
scale measured no sizes, no corpus to grow
{%- else -%}
sizes={{ sizes }} budget_recall_p95={{ budget_recall_p95_ms }}ms
{% for point in points %}  {{
    "size={} facts={} ingest={}ch/s recall_p50={}ms p95={}ms p99={}ms ppr={}ms detect={}ms"
    " store={}b index={}b host={}gb".format(
        point.size, point.facts, point.ingest_chunks_per_s, point.recall_p50_ms,
        point.recall_p95_ms, point.recall_p99_ms, point.ppr_query_ms, point.community_detect_ms,
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
    """The full scaling curve, one row per size and the knee flagged per component.

    sizes: the corpus sizes the curve was measured at, ascending.
    points: the measured row per size, in the same order.
    budget: the per-component ceilings the knees were read against.
    knees: the first size each over-budget component broke its ceiling, the optimization targets.
    """

    sizes: list[int]
    points: list[ScalePoint]
    budget: Budget
    knees: list[Knee]

    def render(self) -> str:
        """Render this curve as a compact text table, one row per size then the flagged knees."""
        points = [
            {
                "size": point.size,
                "facts": point.facts,
                "ingest_chunks_per_s": round(point.ingest_chunks_per_s),
                "recall_p50_ms": round(point.recall_p50_ms, 1),
                "recall_p95_ms": round(point.recall_p95_ms, 1),
                "recall_p99_ms": round(point.recall_p99_ms, 1),
                "ppr_query_ms": round(point.ppr_query_ms, 1),
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
    """Draw one L2-normalized random vector at the stored halfvec width, a synthetic embedding.

    Synthetic unit vectors keep a 100k-row generation off the live embedder while the pgvector
    index and the cosine scan are still exercised at their true shape.

    rng: the seeded generator the whole corpus is drawn from, for a reproducible curve.
    dim: the embedding width, the stored halfvec dimension.
    """
    vector = rng.standard_normal(dim)
    vector /= np.linalg.norm(vector) or 1.0
    return vector.tolist()


def index_id(user_id: uuid.UUID, kind: str, index: int) -> uuid.UUID:
    """Map a row's kind and index to a stable id, so additive growth never collides on a key.

    user_id: the throwaway user namespacing the corpus.
    kind: the row family, such as document, chunk, entity, or fact.
    index: the row's position within its family.
    """
    return uuid.uuid5(user_id, f"{kind}-{index}")


def corpus_rows(
    user_id: uuid.UUID,
    generated: Generated,
    target: CorpusScale,
    rng: np.random.Generator,
    dim: int,
) -> dict[type[TableBase], list[Row]]:
    """Build the additive delta rows per table, documents through facts, so every key resolves.

    Ids derive from the user and the row index, so growth never collides and each user
    namespaces its own corpus.

    user_id: the owner every row is scoped to under row level security.
    generated: the running tally of rows already inserted, the indexes the delta starts at.
    target: the corpus shape to grow to.
    rng: the seeded generator the embeddings are drawn from.
    dim: the embedding width.
    """
    owned: Row = {"owner_id": user_id, "scopes": []}
    documents: list[Row] = [
        {
            "id": index_id(user_id, "document", i),
            "kind": "note",
            "title": f"scale document {i}",
            "content_hash": uuid.uuid5(user_id, f"content-{i}").hex,
            **owned,
        }
        for i in range(generated.documents, target.documents)
    ]
    chunks: list[Row] = [
        {
            "id": index_id(user_id, "chunk", i),
            "document_id": index_id(user_id, "document", i // CHUNKS_PER_DOC),
            "ord": i % CHUNKS_PER_DOC,
            "text": f"scale chunk {i} about entity {i % CHUNKS_PER_ENTITY} and topic {i % 32}",
            "embedding": unit_vector(rng, dim),
            **owned,
        }
        for i in range(generated.chunks, target.chunks)
    ]
    # entity and fact content carry no owner or scope of their own, minted once per namespaced
    # index id; the matching claim in this user's own private container is what makes each
    # one visible and writable to it, the two-row shape every real content/claim write also takes.
    entity_content: list[Row] = [
        {
            "id": index_id(user_id, "entity", i),
            "name": f"entity {i}",
            "type": ontology.CONCEPT,
            "embedding": unit_vector(rng, dim),
        }
        for i in range(generated.entities, target.entities)
    ]
    entity_claims: list[Row] = [
        {
            "id": index_id(user_id, "entity_claim", i),
            "content_id": index_id(user_id, "entity", i),
            "attributes": {},
            **owned,
        }
        for i in range(generated.entities, target.entities)
    ]
    fact_content: list[Row] = [
        {
            "id": index_id(user_id, "fact", i),
            "subject_id": index_id(user_id, "entity", i % target.entities),
            "object_id": None
            if i % 5 == 0
            else index_id(user_id, "entity", (i * 7 + 1) % target.entities),
            "predicate": PREDICATES[i % len(PREDICATES)],
            "statement": (
                f"entity {i % target.entities} {PREDICATES[i % len(PREDICATES)]}"
                f" entity {(i * 7 + 1) % target.entities}"
            ),
            "embedding": unit_vector(rng, dim),
        }
        for i in range(generated.facts, target.facts)
    ]
    fact_claims: list[Row] = [
        {
            "id": index_id(user_id, "fact_claim", i),
            "content_id": index_id(user_id, "fact", i),
            "attributes": {},
            **owned,
        }
        for i in range(generated.facts, target.facts)
    ]
    return {
        Document: documents,
        Chunk: chunks,
        EntityContent: entity_content,
        EntityClaim: entity_claims,
        FactContent: fact_content,
        FactClaim: fact_claims,
    }


async def insert_rows(rows: dict[type[TableBase], list[Row]]) -> None:
    """Insert every table's rows in bounded executemany batches, so a large delta streams in.

    rows: the per-table delta rows to insert, corpus_rows' own output.
    """
    for table, table_rows in rows.items():
        for offset in range(0, len(table_rows), INSERT_BATCH):
            batch = table_rows[offset : offset + INSERT_BATCH]
            await session().execute(insert(table), batch)


def advance_generated(generated: Generated, target: CorpusScale) -> None:
    """Advance the running tally to the target shape just grown to, in place.

    generated: the running tally, mutated to match target.
    target: the corpus shape just grown to.
    """
    for field in Generated.model_fields:
        setattr(generated, field, getattr(target, field))


async def grow_corpus(
    user_id: uuid.UUID,
    generated: Generated,
    target: CorpusScale,
    rng: np.random.Generator,
) -> float:
    """Grow the throwaway corpus to a target size and return the ingestion throughput in chunks/s.

    Inserts only the delta past what was generated, documents then chunks then entities then
    facts so every foreign key resolves, in bounded executemany batches under one user-scoped
    transaction, then advances the running tally. The wall time covers only the new rows so the
    throughput is the marginal ingest rate at this size, not the cumulative one.

    user_id: the throwaway user that owns the corpus.
    generated: the running tally of rows already inserted, advanced in place.
    target: the corpus shape to grow to.
    rng: the seeded generator the embeddings are drawn from.
    """
    rows = corpus_rows(user_id, generated, target, rng, settings.embed_dim)
    start = time.perf_counter()
    async with acting_as(user_id):
        await insert_rows(rows)
    elapsed = time.perf_counter() - start
    advance_generated(generated, target)
    rate = len(rows[Chunk]) / elapsed if elapsed else 0.0
    logger.info(
        "grew corpus to {size} chunks in {elapsed:.2f}s, {rate:.0f} chunks/s",
        size=target.chunks,
        elapsed=elapsed,
        rate=rate,
    )
    return rate


async def measure_lanes(
    query: str,
    vector: list[float],
    k: int,
    repeats: int,
) -> list[LaneLatency]:
    """Time each retrieval lane in isolation on one shared session and return their percentiles.

    Breaks the fused recall into its components, the hybrid vector/lexical scan, the multi-hop
    pagerank lane, the cosine community ranking, and a bare count whose whole cost is the
    row-level-security predicate, so the curve shows which lane bends first as the corpus grows
    rather than only the end-to-end number.

    query: the lexical and embedding query text.
    vector: the query embedding.
    k: how many results each lane surfaces.
    repeats: how many times each lane is timed.
    """
    round_ = Recall(Embedder(), query, vector, k, None, ppr=True)

    async def rank_communities() -> None:
        distance = Community.embedding.cosine_distance(vector)
        ranking = select(Community.label).where(Community.embedding.is_not(None))
        await session().execute(ranking.order_by(distance).limit(k))

    async def scoped_count() -> None:
        await session().scalar(select(func.count()).select_from(Chunk))

    lanes: dict[str, Callable[[], Awaitable[object]]] = {
        "hybrid": round_.hybrid_recall,
        "ppr": round_.ppr_facts,
        "community": rank_communities,
        "rls": scoped_count,
    }
    return [await LaneLatency.timed(name, call, repeats) for name, call in lanes.items()]


async def measure_graph_ops(vector: list[float], repeats: int) -> tuple[float, float]:
    """Time the two graph ops the curve hunts a knee in, pagerank per query and detection in batch.

    Pagerank is seeded from the entities the closest latest facts touch and timed per query, the
    median taken so a single cold walk does not dominate. Community detection is timed once over
    the whole loaded graph, the batch op the weekly pass runs. Both are the networkx CPU walks the
    curve locates the breaking point of, against the Postgres-CTE and cuGraph alternatives.

    vector: the query embedding the seeds are ranked by.
    repeats: how many pagerank queries to time before the median.
    """
    distance = LiveFact.embedding.cosine_distance(vector)
    rows = await session().execute(
        select(LiveFact.subject_id, LiveFact.object_id)
        .where(LiveFact.embedding.is_not(None))
        .order_by(distance)
        .limit(settings.graph_facts_k)
    )
    seeds = list(
        {end for row in rows for end in (row.subject_id, row.object_id) if end is not None}
    )
    ppr = await LaneLatency.timed(
        "ppr", partial(ppr_expand, seeds, settings.graph_facts_k), repeats
    )
    facts = list(await session().scalars(select(LiveFact).where(LiveFact.embedding.is_not(None))))
    start = time.perf_counter()
    detect(facts, settings.community_min_size)
    detect_ms = (time.perf_counter() - start) * 1000.0
    return ppr.p50_ms, detect_ms


async def measure_point(
    user_id: uuid.UUID,
    query: str,
    vector: list[float],
    scale: CorpusScale,
    ingest_chunks_per_s: float,
    k: int,
    repeats: int,
) -> ScalePoint:
    """Measure one corpus size into a curve row, recall, per-lane, graph-op, and storage.

    Times end-to-end recall, then on one shared session breaks it into per-lane latencies and the
    two graph ops, all inside a mainboard meter so the memory peak rides alongside. The storage
    footprint spans every user's rows since a table size is global, so the curve reads the
    growth across sizes rather than an absolute attributable to this corpus alone.

    user_id: the throwaway user whose visibility scopes every read.
    query: the recall and lexical query text.
    vector: the precomputed query embedding the lanes and graph ops are ranked by.
    scale: the corpus shape measured, read for the entity and fact counts.
    ingest_chunks_per_s: the marginal ingest rate measured growing the corpus to this size.
    k: how many hits and seed facts each recall surfaces.
    repeats: how many recall and per-lane calls each percentile is read over.
    """
    with open_meter() as meter:
        recalled = await LaneLatency.timed("recall", partial(recall, query, user_id, k), repeats)
        meter.sample()
        async with acting_as(user_id):
            lanes = await measure_lanes(query, vector, k, repeats)
            ppr_ms, detect_ms = await measure_graph_ops(vector, repeats)
            footprint = (
                await session().execute(
                    text(
                        "SELECT coalesce(sum(pg_total_relation_size(relid)), 0) AS total_bytes, "
                        "coalesce(sum(pg_indexes_size(relid)), 0) AS index_bytes "
                        "FROM pg_stat_user_tables WHERE relname = ANY(:tables)"
                    ),
                    {"tables": list(CORPUS_TABLES)},
                )
            ).one()
            meter.sample()
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
        ppr_query_ms=ppr_ms,
        community_detect_ms=detect_ms,
        storage_bytes=int(footprint.total_bytes or 0),
        index_bytes=int(footprint.index_bytes or 0),
        peak_host_gb=meter.peak_host_gb,
        peak_gpu_gb=meter.peak_gpu_gb,
    )


def find_knees(points: list[ScalePoint], budget: Budget) -> list[Knee]:
    """Flag the first size each tracked component crossed its budget at, the optimization targets.

    A component that never crosses contributes no knee. The lane set is read from whatever the
    points carry rather than hard-coded, so it stays in step with `measure_lanes`.

    points: the measured curve rows, ascending in size.
    budget: the per-component ceilings each series is tested against.
    """
    readers: list[tuple[str, float, Callable[[ScalePoint], float]]] = [
        ("recall_p95", budget.recall_p95_ms, lambda point: point.recall_p95_ms),
        ("ppr_query", budget.ppr_query_ms, lambda point: point.ppr_query_ms),
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


async def purge_user(user_id: uuid.UUID) -> None:
    """Delete the throwaway user and every row it owns, so a scale run leaves no residue.

    Empties the scoped claim tables under the user's own visibility first, then deletes the
    entity and fact content those claims staked as the system user, the one identity the
    content tables' admin-only DELETE policy admits, before dropping the user itself. Every id
    here is namespaced by `uuid.uuid5(user_id, ...)` (`index_id`), so this cleanup can never
    touch a row this throwaway corpus did not itself mint.

    user_id: the throwaway user to remove.
    """
    async with acting_as(user_id):
        entity_content_ids = list(
            await session().scalars(
                select(EntityClaim.content_id).where(EntityClaim.owner_id == user_id)
            )
        )
        fact_content_ids = list(
            await session().scalars(
                select(FactClaim.content_id)
                .where(FactClaim.owner_id == user_id)
                .execution_options(**{settings.skip_live_gate: True})
            )
        )
        for table in (FactClaim, Community, Profile, EntityClaim, Chunk, Document, Watermark):
            await session().execute(delete(table).where(table.owner_id == user_id))
    async with as_system():
        await session().execute(delete(FactContent).where(FactContent.id.in_(fact_content_ids)))
        await session().execute(
            delete(EntityContent).where(EntityContent.id.in_(entity_content_ids))
        )
        await session().execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def measure_size(
    user_id: uuid.UUID,
    query: str,
    vector: list[float],
    size: int,
    generated: Generated,
    rng: np.random.Generator,
    k: int,
    repeats: int,
) -> ScalePoint:
    """Grow the corpus to one size and measure it, logging the curve row for this size.

    user_id: the throwaway user whose corpus is grown and measured.
    query: the recall and lexical query text.
    vector: the precomputed query embedding.
    size: the target chunk count for this row of the curve.
    generated: the running tally of rows already inserted, advanced in place.
    rng: the seeded generator the embeddings are drawn from.
    k: how many hits and seed facts each recall surfaces.
    repeats: how many recall and per-lane calls each percentile is read over.
    """
    scale = CorpusScale.for_size(size)
    ingest_rate = await grow_corpus(user_id, generated, scale, rng)
    point = await measure_point(user_id, query, vector, scale, ingest_rate, k, repeats)
    logger.info(
        "scale size={size} recall_p95={p95:.1f}ms ppr={ppr:.1f}ms detect={det:.1f}ms",
        size=size,
        p95=point.recall_p95_ms,
        ppr=point.ppr_query_ms,
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
    """Grow a throwaway corpus through the sizes and measure the scaling curve, flagging each knee.

    The throwaway user and its rows are purged at the end unless kept, so a run on the live
    database leaves no residue.

    sizes: the ascending corpus sizes to measure, the million left opt-in.
    k: how many hits and seed facts each recall surfaces.
    repeats: how many recall and per-lane calls each percentile is read over.
    query: the probe query every size is recalled with.
    budget: the per-component ceilings the knees are read against, the defaults when null.
    seed: the generator seed, for a reproducible corpus and curve.
    keep: leave the throwaway user and its corpus in place rather than purging them.
    """
    budget = budget or Budget()
    rng = np.random.default_rng(seed)
    [vector] = await Embedder().embed([query], mode="query")
    async with as_system():
        user_id = (await User.create("scale-benchmark")).id
    generated = Generated()
    points: list[ScalePoint] = []
    try:
        for size in sorted(sizes):
            points.append(
                await measure_size(user_id, query, vector, size, generated, rng, k, repeats)
            )
    finally:
        if not keep:
            await purge_user(user_id)
    return ScaleReport(
        sizes=sorted(sizes), points=points, budget=budget, knees=find_knees(points, budget)
    )
