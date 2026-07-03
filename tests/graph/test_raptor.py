import asyncio
import uuid

import pytest
from graphdb import FakeLLM
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import text

from aizk.extract.ontology import EntityType
from aizk.graph.raptor import (
    Node,
    build_raptor,
    cluster,
    cosine,
    raptor_levels,
    raptor_search,
    redundant_parent,
    target_level,
)
from aizk.store import Community, EntityClaim, EntityContent, acting_as

DIM = 1024


def basis(index: int) -> list[float]:
    """A unit vector with its single one in the index slot, so two such vectors are orthogonal.

    index: position of the single one, the axis the seeded embedding points along.
    """
    slots = [0.0] * DIM
    slots[index] = 1.0
    return slots


# components bounded away from zero and from underflow, so a self-cosine of one is the alignment
# under test rather than the floating-point norm collapsing to zero on a subnormal magnitude.
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


def test_cluster_splits_two_orthogonal_groups() -> None:
    """Two tight groups along orthogonal axes cluster apart, none crossing the similarity link."""
    embeddings = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.1, 0.9]]
    groups = cluster(embeddings, threshold=0.5)
    assert {frozenset(group) for group in groups} == {frozenset({0, 1}), frozenset({2, 3})}


def test_cluster_without_links_returns_singletons() -> None:
    """With no pair clearing the threshold every node is its own group, the climb's stop signal."""
    assert cluster([[1.0, 0.0], [0.0, 1.0]], threshold=0.9) == [[0], [1]]


def test_target_level_climbs_broad_pins_specific_and_lands_mid_in_between() -> None:
    """Over three levels a theme reads root, a pinned query the leaf, and the rest a middle tier.

    A thematic query climbs to the broadest root regardless of its wording, a query carrying a
    specificity marker or naming two things drops to the finest leaf, and a plain mid-abstraction
    query lands on the middle tier, so the intermediate summaries become reachable rather than the
    old broad-or-leaf snap.
    """
    levels = [1, 2, 3]

    assert target_level(levels, "an overview of everything", thematic=True) == 3
    assert target_level(levels, "the exact value of the constant", thematic=False) == 1
    assert target_level(levels, "how Alpha relates to Beta", thematic=False) == 1
    assert target_level(levels, "how the methods work", thematic=False) == 2


def test_target_level_collapses_the_middle_onto_the_leaf_with_two_levels() -> None:
    """With only two levels there is no middle, so a non-thematic query still reads the leaf."""
    assert target_level([1, 2], "how the methods work", thematic=False) == 1
    assert target_level([1, 2], "an overview", thematic=True) == 2


def test_redundant_parent_finds_a_near_duplicate_else_none() -> None:
    """A near-duplicate parent is reused while a distinct one returns null, the DTCRS prune."""
    kept = Node(entity_id=uuid.uuid4(), label="theme", summary="a paragraph", embedding=[1.0, 0.0])
    parents = [(kept, [1.0, 0.0])]
    assert redundant_parent(parents, [0.99, 0.01], threshold=0.95) is kept
    assert redundant_parent(parents, [0.0, 1.0], threshold=0.95) is None


async def seed_communities(owner: uuid.UUID) -> None:
    """Plant four communities on two orthogonal axes, the leaves the tree climbs above.

    owner: principal that owns the communities.
    """
    pairs = [("Alpha", basis(0)), ("Beta", basis(0)), ("Gamma", basis(1)), ("Delta", basis(1))]
    async with acting_as(owner) as session:
        for label, vector in pairs:
            session.add(
                Community(
                    id=uuid.uuid4(),
                    owner_id=owner,
                    label=label,
                    summary=f"{label} covers its own area",
                    embedding=vector,
                )
            )


@pytest.mark.usefixtures("fake_embedder")
def test_build_raptor_lifts_communities_into_a_part_of_tree(
    fresh_principal: uuid.UUID, fake_llm: FakeLLM
) -> None:
    """Four communities become level-0 leaves under at least one part_of-linked parent summary.

    The leaves cluster two-and-two and roll up, every leaf gains a part_of edge to its parent, a
    redundant rollup is reused rather than minting a twin, so the written count equals the parents
    and every edge climbs from a leaf to a higher level. Building a second time first clears this
    principal's own prior tree, the content/claim removal `leaf_nodes` runs before minting fresh
    leaves, so a rebuild never doubles the tree it already owns.
    """
    owner = fresh_principal

    async def probe() -> tuple[int, int, int, list[tuple[int, int]]]:
        await seed_communities(owner)
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

    written, leaves, parents, edges = asyncio.run(probe())
    assert leaves == 4
    assert parents >= 1
    assert written == parents
    assert len(edges) == 4
    assert all(child == 0 and parent >= 1 for child, parent in edges)


async def seed_two_levels(owner: uuid.UUID) -> None:
    """Plant one summary entity at level 1 and one at level 2, a fixed tree for the retrieval read.

    owner: principal that owns the summary entities.
    """
    async with acting_as(owner) as session:
        for level, label in ((1, "leaf summary"), (2, "root summary")):
            content = EntityContent(name=label, type=EntityType.RAPTOR_SUMMARY, embedding=basis(0))
            session.add(content)
            await session.flush()
            session.add(
                EntityClaim(
                    content_id=content.id,
                    owner_id=owner,
                    attributes={"level": level, "summary": f"{label} text"},
                )
            )


@pytest.mark.usefixtures("fake_embedder")
def test_raptor_search_picks_the_level_by_query_breadth(
    fresh_principal: uuid.UUID,
) -> None:
    """A broad query reaches the root level while a pointed query reads the leaf summaries."""
    owner = fresh_principal

    async def probe() -> tuple[list[int], list[int], list[int]]:
        await seed_two_levels(owner)
        levels = await raptor_levels(owner)
        broad = await raptor_search(
            "an overview of the whole area", principal_id=owner, thematic=True
        )
        pointed = await raptor_search("one specific detail", principal_id=owner, thematic=False)
        return levels, [lvl for _, _, lvl, _ in broad], [lvl for _, _, lvl, _ in pointed]

    levels, broad_levels, pointed_levels = asyncio.run(probe())
    assert levels == [1, 2]
    assert broad_levels == [2]
    assert pointed_levels == [1]


@pytest.mark.usefixtures("fake_embedder")
def test_raptor_search_on_an_unbuilt_tree_returns_nothing(
    fresh_principal: uuid.UUID,
) -> None:
    """With no summary levels yet, a query short-circuits to an empty result, never ranking."""
    owner = fresh_principal
    assert asyncio.run(raptor_search("anything", principal_id=owner)) == []


async def seed_one_merge_and_two_singletons(owner: uuid.UUID) -> None:
    """Plant four communities where two share an axis and two stand alone, a mixed clustering.

    Two communities on one axis link into a merged cluster while the other two on distinct axes
    stay singletons, so a level built from this clustering both rolls up a parent and carries the
    lone nodes up unchanged, the singleton-passthrough prune.

    owner: principal that owns the communities.
    """
    pairs = [("Alpha", basis(0)), ("Beta", basis(0)), ("Gamma", basis(1)), ("Delta", basis(2))]
    async with acting_as(owner) as session:
        for label, vector in pairs:
            session.add(
                Community(
                    id=uuid.uuid4(),
                    owner_id=owner,
                    label=label,
                    summary=f"{label} covers its own area",
                    embedding=vector,
                )
            )


@pytest.mark.usefixtures("fake_embedder")
def test_build_raptor_carries_a_singleton_up_beside_a_merged_cluster(
    fresh_principal: uuid.UUID, fake_llm: FakeLLM
) -> None:
    """A level that merges one pair and leaves two singletons rolls up a parent and prunes no node.

    The merged pair mints exactly one summary while each lone community is carried up unchanged, so
    the climb writes one parent and never a summary that merely restates a single child.
    """
    owner = fresh_principal

    async def probe() -> int:
        await seed_one_merge_and_two_singletons(owner)
        return await build_raptor(principal_id=owner)

    assert asyncio.run(probe()) == 1


@pytest.mark.usefixtures("fake_embedder")
def test_build_raptor_stops_when_a_level_merges_nothing(
    fresh_principal: uuid.UUID, fake_llm: FakeLLM
) -> None:
    """Communities on distinct axes never link, so the first clustering past the root cap merges
    nothing and the climb stops with no summaries written above the leaves."""
    owner = fresh_principal

    async def probe() -> int:
        async with acting_as(owner) as session:
            for index in range(4):
                session.add(
                    Community(
                        id=uuid.uuid4(),
                        owner_id=owner,
                        label=f"c{index}",
                        summary=f"area {index}",
                        embedding=basis(index),
                    )
                )
        return await build_raptor(principal_id=owner)

    assert asyncio.run(probe()) == 0


@pytest.mark.usefixtures("fake_embedder")
def test_build_raptor_on_too_few_communities_writes_nothing(
    fresh_principal: uuid.UUID, fake_llm: FakeLLM
) -> None:
    """Fewer than two communities cannot cluster, so the climb writes no summaries."""
    owner = fresh_principal

    async def probe() -> int:
        async with acting_as(owner) as session:
            session.add(
                Community(
                    id=uuid.uuid4(),
                    owner_id=owner,
                    label="lonely",
                    summary="only one",
                    embedding=basis(0),
                )
            )
        return await build_raptor(principal_id=owner)

    assert asyncio.run(probe()) == 0
