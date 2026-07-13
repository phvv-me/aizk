import uuid
from collections.abc import Iterator

import dbutil
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pgvector import HalfVector
from sqlalchemy import text

from aizk.graph.raptor import (
    Node,
    build_raptor,
    cluster,
    cosine,
    redundant_parent,
    to_floats,
)
from aizk.store import Community

DIM = 1024


@pytest.fixture
def owner(migrated_db: None) -> Iterator[uuid.UUID]:
    pid = uuid.uuid4()

    async def setup() -> None:
        await dbutil.reset_db()

    dbutil.run(setup())
    yield pid


def basis(index: int) -> list[float]:
    slots = [0.0] * DIM
    slots[index] = 1.0
    return slots


# Components stay away from underflow so self-cosine tests alignment.
small_vectors = st.lists(
    st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_subnormal=False),
    min_size=2,
    max_size=6,
)


@given(vector=small_vectors)
def test_cosine_of_a_vector_with_itself_is_one(vector: list[float]) -> None:
    assert cosine(vector, vector) == pytest.approx(1.0)


def test_cosine_reads_orthogonal_and_degenerate_pairs() -> None:
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cluster_splits_groups_and_falls_back_to_singletons() -> None:
    embeddings = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.1, 0.9]]
    groups = cluster(embeddings, threshold=0.5)
    assert {frozenset(group) for group in groups} == {frozenset({0, 1}), frozenset({2, 3})}
    assert cluster([[1.0, 0.0], [0.0, 1.0]], threshold=0.9) == [[0], [1]]


def test_redundant_parent_finds_a_near_duplicate_else_none() -> None:
    kept = Node(entity_id=uuid.uuid4(), label="theme", summary="a paragraph", embedding=[1.0, 0.0])
    parents = [(kept, [1.0, 0.0])]
    assert redundant_parent(parents, [0.99, 0.01], threshold=0.95) is kept
    assert redundant_parent(parents, [0.0, 1.0], threshold=0.95) is None


def test_to_floats_unwraps_a_halfvector_and_passes_a_list_through() -> None:
    assert to_floats([0.5, 0.25]) == [0.5, 0.25]
    unwrapped = to_floats(HalfVector([1.0, 0.0]))
    assert isinstance(unwrapped, list) and unwrapped == pytest.approx([1.0, 0.0])


async def seed_communities(owner: uuid.UUID, axes: list[int]) -> None:
    async with dbutil.actor(owner) as session:
        for index, axis in enumerate(axes):
            session.add(
                Community(
                    id=uuid.uuid4(),
                    created_by=owner,
                    scopes=[owner],
                    label=f"c{index}",
                    summary=f"community {index} covers its area",
                    embedding=basis(axis),
                )
            )


@pytest.mark.usefixtures("fake_embedder")
def test_build_raptor_lifts_communities_into_a_part_of_tree(
    owner: uuid.UUID, fake_llm: object
) -> None:
    async def probe() -> tuple[int, int, int, list[tuple[int, int]]]:
        await seed_communities(owner, [0, 0, 1, 1])
        await build_raptor(scopes=frozenset({owner}))
        written = await build_raptor(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            leaves = (
                await session.exec(
                    text(
                        "SELECT count(*) FROM entity_claim ec "
                        "JOIN entity_content ent ON ent.id = ec.content_id "
                        "WHERE ec.scopes = CAST(:scopes AS uuid[]) "
                        "AND ent.type = 'raptor_summary' "
                        "AND (ec.attributes->>'level')::int = 0"
                    ),
                    params={"scopes": [str(owner)]},
                )
            ).scalar_one()
            parents = (
                await session.exec(
                    text(
                        "SELECT count(*) FROM entity_claim ec "
                        "JOIN entity_content ent ON ent.id = ec.content_id "
                        "WHERE ec.scopes = CAST(:scopes AS uuid[]) "
                        "AND ent.type = 'raptor_summary' "
                        "AND (ec.attributes->>'level')::int >= 1"
                    ),
                    params={"scopes": [str(owner)]},
                )
            ).scalar_one()
            rows = await session.exec(
                text(
                    "SELECT (c.attributes->>'level')::int AS child_level, "
                    "(p.attributes->>'level')::int AS parent_level FROM fact_claim f "
                    "JOIN fact_content fc ON fc.id = f.content_id "
                    "JOIN entity_claim c "
                    "ON c.content_id = fc.subject_id AND c.scopes = f.scopes "
                    "JOIN entity_claim p "
                    "ON p.content_id = fc.object_id AND p.scopes = f.scopes "
                    "WHERE f.scopes = CAST(:scopes AS uuid[]) AND fc.predicate = 'part_of'"
                ),
                params={"scopes": [str(owner)]},
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
    async def probe() -> int:
        await seed_communities(owner, axes)
        return await build_raptor(scopes=frozenset({owner}))

    assert dbutil.run(probe()) == expected
