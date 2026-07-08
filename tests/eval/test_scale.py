import math
import uuid

import dbutil
import numpy as np
import pytest
from doubles import RecordingEmbedder
from hypothesis import given
from hypothesis import strategies as st

from aizk.config import settings
from aizk.eval.scale import (
    CHUNKS_PER_DOC,
    CHUNKS_PER_ENTITY,
    Budget,
    CorpusScale,
    Generated,
    Knee,
    LaneLatency,
    ScalePoint,
    ScaleReport,
    corpus_rows,
    find_knees,
    index_id,
    run_scale_benchmark,
    unit_vector,
)
from aizk.store import Chunk, Document, EntityClaim, EntityContent, FactClaim, FactContent


def point(
    size: int,
    recall_p95_ms: float = 0.0,
    ppr_query_ms: float = 0.0,
    community_detect_ms: float = 0.0,
    lane_p95_ms: float = 0.0,
) -> ScalePoint:
    """A synthetic curve row carrying the latencies under test and zeros elsewhere.

    size: the corpus size the row reports.
    recall_p95_ms: the tail recall latency the knee logic reads.
    ppr_query_ms: the pagerank graph-op latency.
    community_detect_ms: the community-detection batch latency.
    lane_p95_ms: the tail latency of the single vector lane the row carries.
    """
    return ScalePoint(
        size=size,
        entities=size // 4,
        facts=size,
        ingest_chunks_per_s=100.0,
        ingest_facts_per_s=100.0,
        recall_p50_ms=recall_p95_ms / 2,
        recall_p95_ms=recall_p95_ms,
        recall_p99_ms=recall_p95_ms,
        lanes=[LaneLatency(name="vector", p50_ms=0.0, p95_ms=lane_p95_ms, p99_ms=lane_p95_ms)],
        ppr_query_ms=ppr_query_ms,
        community_detect_ms=community_detect_ms,
        storage_bytes=size * 2048,
        index_bytes=size * 1024,
        peak_host_gb=1.0,
        peak_gpu_gb=0.0,
    )


@given(size=st.integers(min_value=1, max_value=2_000_000))
def test_corpus_scale_derives_consistent_counts_from_one_size(size: int) -> None:
    """One chunk count fixes documents, entities, and facts so every chunk has a parent node."""
    scale = CorpusScale.for_size(size)

    assert scale.chunks == size and scale.facts == size
    assert scale.documents == math.ceil(size / CHUNKS_PER_DOC)
    assert scale.documents * CHUNKS_PER_DOC >= size
    assert scale.entities == max(2, size // CHUNKS_PER_ENTITY)


@given(
    dim=st.integers(min_value=1, max_value=64),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
def test_unit_vector_has_the_stored_width_and_unit_norm(dim: int, seed: int) -> None:
    """A generated vector matches the halfvec width and carries unit length at every width."""
    vector = unit_vector(np.random.default_rng(seed), dim)

    assert len(vector) == dim
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-6


@given(
    users=st.lists(st.uuids(version=4), min_size=2, max_size=2, unique=True),
    size=st.integers(min_value=1, max_value=40),
    dim=st.sampled_from([256, 512, 1024]),
)
def test_corpus_rows_are_deterministic_and_structurally_sound(
    users: list[uuid.UUID], size: int, dim: int
) -> None:
    """Rows key by user and index, pack chunks under documents, and never dangle an edge."""
    user, other = users
    scale = CorpusScale.for_size(size)
    rows = corpus_rows(user, Generated(), scale, np.random.default_rng(0), dim)
    again = corpus_rows(user, Generated(), scale, np.random.default_rng(0), dim)

    documents = rows[Document]
    assert [row["id"] for row in documents] == [row["id"] for row in again[Document]]
    # content carries no owner of its own, so only the per-tenant families carry an owner_id
    owned_families = (Document, Chunk, EntityClaim, FactClaim)
    assert all(row["owner_id"] == user for table in owned_families for row in rows[table])
    assert len({row["content_hash"] for row in documents}) == scale.documents  # none dedupes away
    # a different user namespaces a different id for the same index
    other_rows = corpus_rows(other, Generated(), scale, np.random.default_rng(0), dim)
    assert documents[0]["id"] != other_rows[Document][0]["id"]

    chunks = rows[Chunk]
    widths = [
        len(embedding) for chunk in chunks if isinstance(embedding := chunk["embedding"], list)
    ]
    assert widths == [dim] * scale.chunks
    for index, chunk in enumerate(chunks):
        assert chunk["document_id"] == index_id(user, "document", index // CHUNKS_PER_DOC)

    entity_ids = {row["id"] for row in rows[EntityContent]}
    for fact in rows[FactContent]:
        assert fact["subject_id"] in entity_ids
        assert fact["object_id"] is None or fact["object_id"] in entity_ids
    fact_content_ids = {row["id"] for row in rows[FactContent]}
    for claim in rows[FactClaim]:
        assert claim["content_id"] in fact_content_ids  # every claim stakes a real content row


@given(size=st.integers(min_value=1, max_value=40))
def test_corpus_rows_grow_additively_without_key_collisions(size: int) -> None:
    """The delta past a grown tally starts where the first batch stopped, no id inserted twice."""
    user = uuid.uuid5(uuid.NAMESPACE_DNS, "scale-test")
    rng = np.random.default_rng(0)
    first = CorpusScale.for_size(size)
    second = CorpusScale.for_size(size * 2)

    base = corpus_rows(user, Generated(), first, rng, 256)
    delta = corpus_rows(user, Generated(**first.model_dump()), second, rng, 256)

    for table in (Document, Chunk, EntityContent, FactContent, EntityClaim, FactClaim):
        base_ids = {row["id"] for row in base[table]}
        delta_ids = {row["id"] for row in delta[table]}
        assert base_ids.isdisjoint(delta_ids)
    entity_ids = {row["id"] for row in base[EntityContent]} | {
        row["id"] for row in delta[EntityContent]
    }
    for fact in delta[FactContent]:
        assert fact["subject_id"] in entity_ids  # delta edges resolve across both batches


def test_generated_starts_empty_so_growth_is_purely_additive() -> None:
    """The running tally opens at zero on every family, so the first size inserts the delta."""
    generated = Generated()

    assert (generated.documents, generated.chunks, generated.entities, generated.facts) == (
        0,
        0,
        0,
        0,
    )


def test_lane_latency_timed_reads_three_ascending_percentiles() -> None:
    """Timing a call reduces its wall times to named, non-decreasing p50, p95, and p99."""

    async def noop() -> None:
        return None

    lane = dbutil.run(LaneLatency.timed("vector", noop, repeats=5))

    assert lane.name == "vector"
    assert 0.0 <= lane.p50_ms <= lane.p95_ms <= lane.p99_ms


@st.composite
def scale_curves(draw: st.DrawFn) -> tuple[list[ScalePoint], Budget]:
    """An ascending curve and a budget, the input `find_knees` flags the first crossing over.

    Sizes climb, every tracked latency is drawn across the budget band so a component crosses at
    some sizes and not others, and a single vector lane carries its own tail, so the property pins
    the first-over-budget rule across recall, both graph ops, and the per-lane series at once.
    """
    sizes = draw(
        st.lists(st.integers(min_value=1, max_value=10**6), min_size=1, max_size=5, unique=True)
    )
    latency = st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False)
    points = [
        point(
            size,
            recall_p95_ms=draw(latency),
            ppr_query_ms=draw(latency),
            community_detect_ms=draw(latency),
            lane_p95_ms=draw(latency),
        )
        for size in sorted(sizes)
    ]
    limit = st.floats(min_value=50.0, max_value=300.0, allow_nan=False, allow_infinity=False)
    budget = Budget(
        recall_p95_ms=draw(limit),
        lane_p95_ms=draw(limit),
        ppr_query_ms=draw(limit),
        community_detect_ms=draw(limit),
    )
    return points, budget


@given(curve=scale_curves())
def test_find_knees_flags_each_components_first_crossing(
    curve: tuple[list[ScalePoint], Budget],
) -> None:
    """Each tracked component is named at the smallest size it broke its ceiling, none else."""
    points, budget = curve
    readers = {
        "recall_p95": (budget.recall_p95_ms, lambda p: p.recall_p95_ms),
        "ppr_query": (budget.ppr_query_ms, lambda p: p.ppr_query_ms),
        "community_detect": (budget.community_detect_ms, lambda p: p.community_detect_ms),
        "lane:vector": (budget.lane_p95_ms, lambda p: p.lane_p95("vector")),
    }
    expected = {}
    for component, (limit, read) in readers.items():
        breach = next((p for p in points if read(p) > limit), None)
        if breach is not None:
            expected[component] = (breach.size, read(breach), limit)

    knees = find_knees(points, budget)

    assert all(isinstance(knee, Knee) for knee in knees)
    assert {
        knee.component: (knee.size, knee.value_ms, knee.budget_ms) for knee in knees
    } == expected


def test_scale_point_lane_p95_reads_a_present_lane_and_zeros_an_absent_one() -> None:
    """A curve row reads a measured lane's tail and returns zero for a lane it never carried."""
    row = point(1000, lane_p95_ms=42.0)

    assert row.lane_p95("vector") == 42.0
    assert row.lane_p95("missing") == 0.0


@pytest.mark.parametrize(
    ("report", "needles"),
    [
        (
            ScaleReport(
                sizes=[1000],
                points=[point(1000, recall_p95_ms=300.0, ppr_query_ms=10.0)],
                budget=Budget(),
                knees=find_knees([point(1000, recall_p95_ms=300.0)], Budget()),
            ),
            ["size=1000", "p95=300.0ms", "vector_p95=", "knee recall_p95 at size=1000"],
        ),
        (ScaleReport(sizes=[], points=[], budget=Budget(), knees=[]), ["no corpus"]),
        (
            ScaleReport(
                sizes=[1000], points=[point(1000, recall_p95_ms=50.0)], budget=Budget(), knees=[]
            ),
            ["no knee"],
        ),
    ],
    ids=["knee", "empty", "within-budget"],
)
def test_render_renders_the_curve_lane_and_knees(report: ScaleReport, needles: list[str]) -> None:
    """The rendered curve carries each size's metrics, the lane line, and the knee verdict."""
    rendered = report.render()

    assert all(needle in rendered for needle in needles)


@pytest.mark.parametrize("keep", [False, True], ids=["purged", "kept"])
def test_run_scale_benchmark_measures_a_tiny_curve(
    migrated_db: None,
    fake_embedder: RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
    keep: bool,
) -> None:
    """A tiny run grows a real corpus, measures the curve off Postgres, and honors the keep flag.

    The scale lane reaches only Postgres and the corpus is synthetic vectors, so the one live embed
    call, the probe query, runs against the fake seam rather than a co-resident model.
    """

    async def body() -> None:
        await dbutil.reset_db()
        for field in ("rerank", "raptor", "profiles", "recall_gap_fill"):
            monkeypatch.setattr(settings, field, False)
        sizes = (20, 40) if not keep else (20,)
        report = await run_scale_benchmark(sizes=sizes, k=4, repeats=2, budget=Budget(), keep=keep)

        assert [pt.size for pt in report.points] == list(sizes)
        for pt in report.points:
            assert pt.recall_p95_ms >= 0.0
            assert {lane.name for lane in pt.lanes} == {"hybrid", "ppr", "community", "rls"}
            assert pt.storage_bytes > 0 and pt.facts == pt.size
        if len(sizes) == 2:
            assert report.points[1].facts > report.points[0].facts  # the corpus genuinely grew

    dbutil.run(body())
