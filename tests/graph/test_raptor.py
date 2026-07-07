import uuid
from collections.abc import Iterator

import dbutil
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pgvector.utils import HalfVector
from sqlalchemy import text

from aizk.extract import ontology
from aizk.graph.raptor import (
    Node,
    build_raptor,
    cluster,
    cosine,
    raptor_levels,
    raptor_search,
    redundant_parent,
    target_level,
    to_floats,
)
from aizk.store import Community, EntityClaim, EntityContent, acting_as

DIM = 1024


@pytest.fixture
def owner(migrated_db: None) -> Iterator[uuid.UUID]:
    """A freshly reset schema seeding one principal, the owner every tree body climbs under."""
    pid = uuid.uuid4()

    async def setup() -> None:
        await dbutil.reset_db()
        await dbutil.seed_principal(pid)

    dbutil.run(setup())
    yield pid


def basis(index: int) -> list[float]:
    """A unit vector with its single one in the index slot, so two such vectors are orthogonal."""
    slots = [0.0] * DIM
    slots[index] = 1.0
    return slots


# components bounded away from zero and from underflow, so a self-cosine of one is the alignment
# under test rather than the floating-point norm collapsing on a subnormal magnitude.
small_vectors = st.lists(
    st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_subnormal=False),
    min_size=2,
    max_size=6,
)


@given(vector=small_vectors)
def test_cosine_of_a_vector_with_itself_is_one(vector: list[float]) -> None:
    """A nonzero vector is perfectly aligned with itself, the clustering self-similarity anchor."""
    assert cosine(vector, vector) == pytest.approx(1.0)


def test_cosine_reads_orthogonal_and_degenerate_pairs() -> None:
    """Cosine is zero across orthogonal axes and zero when either side has no magnitude."""
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cluster_splits_groups_and_falls_back_to_singletons() -> None:
    """Two tight orthogonal groups cluster apart, and with no link every node stands alone."""
    embeddings = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.1, 0.9]]
    groups = cluster(embeddings, threshold=0.5)
    assert {frozenset(group) for group in groups} == {frozenset({0, 1}), frozenset({2, 3})}
    assert cluster([[1.0, 0.0], [0.0, 1.0]], threshold=0.9) == [[0], [1]]


@pytest.mark.parametrize(
    ("levels", "query", "thematic", "expected"),
    [
        ([1, 2, 3], "an overview of everything", True, 3),
        ([1, 2, 3], "the exact value of the constant", False, 1),
        ([1, 2, 3], "how Alpha relates to Beta", False, 1),
        ([1, 2, 3], "how the methods work", False, 2),
        ([1, 2], "how the methods work", False, 1),
        ([1, 2], "an overview", True, 2),
    ],
)
def test_target_level_reads_breadth_and_specificity(
    levels: list[int], query: str, thematic: bool, expected: int
) -> None:
    """A theme climbs to the root, a marker or two named terms pins the leaf, the rest lands mid.

    The two-level rows also pin the middle collapsing onto the leaf, so the same table covers the
    named-term count, the specificity markers, and the mid-tier split in one parametrization.
    """
    assert target_level(levels, query, thematic) == expected


def test_redundant_parent_finds_a_near_duplicate_else_none() -> None:
    """A near-duplicate parent is reused while a distinct one returns null, the DTCRS prune."""
    kept = Node(entity_id=uuid.uuid4(), label="theme", summary="a paragraph", embedding=[1.0, 0.0])
    parents = [(kept, [1.0, 0.0])]
    assert redundant_parent(parents, [0.99, 0.01], threshold=0.95) is kept
    assert redundant_parent(parents, [0.0, 1.0], threshold=0.95) is None


def test_to_floats_unwraps_a_halfvector_and_passes_a_list_through() -> None:
    """A stored HalfVector reads back as a plain float list, a list passing through unchanged."""
    assert to_floats([0.5, 0.25]) == [0.5, 0.25]
    unwrapped = to_floats(HalfVector([1.0, 0.0]))
    assert isinstance(unwrapped, list) and unwrapped == pytest.approx([1.0, 0.0])


async def seed_communities(owner: uuid.UUID, axes: list[int]) -> None:
    """Plant one community per axis index, the level-0 leaves the tree climbs above."""
    async with acting_as(owner) as session:
        for index, axis in enumerate(axes):
            session.add(
                Community(
                    id=uuid.uuid4(),
                    owner_id=owner,
                    label=f"c{index}",
                    summary=f"community {index} covers its area",
                    embedding=basis(axis),
                )
            )


@pytest.mark.usefixtures("fake_embedder")
def test_build_raptor_lifts_communities_into_a_part_of_tree(
    owner: uuid.UUID, fake_llm: object
) -> None:
    """Four communities become level-0 leaves under a part_of-linked parent, no rebuild doubling.

    The leaves cluster two-and-two and roll up, every leaf gains a part_of edge climbing to a
    higher level, and building a second time first clears this principal's own prior tree, so a
    rebuild never doubles the tree it already owns.
    """

    async def probe() -> tuple[int, int, int, list[tuple[int, int]]]:
        await seed_communities(owner, [0, 0, 1, 1])
        await build_raptor(principal_id=owner)
        written = await build_raptor(principal_id=owner)
        async with acting_as(owner) as session:
            leaves = await session.scalar(
                text(
                    "SELECT count(*) FROM entity_claim ec "
                    "JOIN entity_content ent ON ent.id = ec.content_id "
                    "WHERE ec.owner_id = :o AND ent.type = 'RaptorSummary' "
                    "AND (ec.attributes->>'level')::int = 0"
                ),
                {"o": owner},
            )
            parents = await session.scalar(
                text(
                    "SELECT count(*) FROM entity_claim ec "
                    "JOIN entity_content ent ON ent.id = ec.content_id "
                    "WHERE ec.owner_id = :o AND ent.type = 'RaptorSummary' "
                    "AND (ec.attributes->>'level')::int >= 1"
                ),
                {"o": owner},
            )
            rows = await session.execute(
                text(
                    "SELECT (c.attributes->>'level')::int AS child_level, "
                    "(p.attributes->>'level')::int AS parent_level FROM fact_claim f "
                    "JOIN fact_content fc ON fc.id = f.content_id "
                    "JOIN entity_claim c "
                    "ON c.content_id = fc.subject_id AND c.owner_id = f.owner_id "
                    "JOIN entity_claim p "
                    "ON p.content_id = fc.object_id AND p.owner_id = f.owner_id "
                    "WHERE f.owner_id = :o AND fc.predicate = 'part_of'"
                ),
                {"o": owner},
            )
            edges = [(row.child_level, row.parent_level) for row in rows]
        return written, leaves or 0, parents or 0, edges

    written, leaves, parents, edges = dbutil.run(probe())
    assert leaves == 4
    assert parents >= 1
    assert written == parents
    assert len(edges) == 4
    assert all(child == 0 and parent >= 1 for child, parent in edges)


@pytest.mark.usefixtures("fake_embedder")
@pytest.mark.parametrize(
    ("axes", "expected"),
    [
        ([0, 0, 1, 2], 1),  # one merged pair mints a parent, two singletons carry up unchanged
        ([0, 1, 2, 3], 0),  # distinct axes never link, the first clustering merges nothing
        ([0], 0),  # fewer than two communities cannot cluster at all
    ],
)
def test_build_raptor_writes_the_expected_summary_count(
    owner: uuid.UUID, fake_llm: object, axes: list[int], expected: int
) -> None:
    """The climb mints one summary per merged cluster, carries singletons up, stops on no merge.

    The three rows cover the singleton-passthrough prune beside a real rollup, the merge-nothing
    stop once a clustering cannot shrink the level, and the too-few-leaves short circuit.
    """

    async def probe() -> int:
        await seed_communities(owner, axes)
        return await build_raptor(principal_id=owner)

    assert dbutil.run(probe()) == expected


async def seed_two_levels(owner: uuid.UUID) -> None:
    """Plant one summary entity at level 1 and one at level 2, a fixed tree the read retrieves."""
    async with acting_as(owner) as session:
        for level, label in ((1, "leaf summary"), (2, "root summary")):
            content = EntityContent(name=label, type=ontology.RAPTOR_SUMMARY, embedding=basis(0))
            session.add(content)
            await session.flush()
            session.add(
                EntityClaim(
                    content_id=content.id,
                    owner_id=owner,
                    attributes={"level": level, "summary": f"{label} text"},
                )
            )


def test_raptor_search_picks_the_level_by_query_breadth(owner: uuid.UUID) -> None:
    """A broad query reaches the root level while a pointed query reads the leaf summaries."""

    async def probe() -> tuple[list[int], list[int], list[int]]:
        await seed_two_levels(owner)
        async with acting_as(owner) as session:
            levels = await raptor_levels(session)
            broad = await raptor_search(
                session, "an overview of the whole area", basis(0), thematic=True
            )
            pointed = await raptor_search(session, "one specific detail", basis(0), thematic=False)
        return levels, [lvl for _, _, lvl, _ in broad], [lvl for _, _, lvl, _ in pointed]

    levels, broad_levels, pointed_levels = dbutil.run(probe())
    assert levels == [1, 2]
    assert broad_levels == [2]
    assert pointed_levels == [1]


def test_raptor_search_on_an_unbuilt_tree_returns_nothing(owner: uuid.UUID) -> None:
    """With no summary levels yet a query short-circuits to an empty result, never ranking."""

    async def probe() -> list[tuple[str, str, int, float]]:
        async with acting_as(owner) as session:
            return await raptor_search(session, "anything", basis(0))

    assert dbutil.run(probe()) == []
