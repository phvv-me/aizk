from collections.abc import Iterator

import dbutil
import pytest
from doubles import FakeLLM, RecordingEmbedder
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5
from pgvector import HalfVector
from pydantic import UUID5, UUID7
from sqlalchemy import text
from sqlmodel import select

from aizk.graph.raptor import (
    Node,
    RaptorBuilder,
    build_raptor,
    cosine,
    redundant_parent,
    to_floats,
)
from aizk.store import Community, Entity

DIM = 1024


@pytest.fixture
def owner(migrated_db: None) -> Iterator[UUID5 | UUID7]:
    pid = uuid5()

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
def test_cosine_handles_reflexive_orthogonal_and_degenerate_vectors(vector: list[float]) -> None:
    assert cosine(vector, vector) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_redundant_parent_finds_a_near_duplicate_else_none() -> None:
    kept = Node(entity_id=uuid5(), label="theme", summary="a paragraph", embedding=[1.0, 0.0])
    parents = [(kept, [1.0, 0.0])]
    assert redundant_parent(parents, [0.99, 0.01], threshold=0.95) is kept
    assert redundant_parent(parents, [0.0, 1.0], threshold=0.95) is None


def test_to_floats_unwraps_a_halfvector_and_passes_a_list_through() -> None:
    assert to_floats([0.5, 0.25]) == [0.5, 0.25]
    unwrapped = to_floats(HalfVector([1.0, 0.0]))
    assert isinstance(unwrapped, list) and unwrapped == pytest.approx([1.0, 0.0])


async def seed_communities(owner: UUID5 | UUID7, axes: list[int]) -> None:
    async with dbutil.actor(owner) as session:
        for index, axis in enumerate(axes):
            session.add(
                Community(
                    id=uuid5(),
                    created_by=owner,
                    scopes=[owner],
                    label=f"c{index}",
                    summary=f"community {index} covers its area",
                    embedding=basis(axis),
                )
            )


def test_similarity_groups_preserves_one_vector(owner: UUID5 | UUID7) -> None:
    builder = RaptorBuilder(
        scopes=frozenset({owner}), llm=FakeLLM().llm, embed=RecordingEmbedder()
    )
    assert dbutil.run(builder.similarity_groups([basis(0)])) == [[0]]


def test_build_raptor_lifts_communities_into_a_part_of_tree(
    owner: UUID5 | UUID7, fake_llm: FakeLLM, fake_embedder: RecordingEmbedder
) -> None:
    async def probe() -> tuple[int, int, int, list[tuple[int, int]]]:
        await seed_communities(owner, [0, 0, 1, 1])
        await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))
        written = await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))
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


def test_build_raptor_shares_content_without_deleting_another_scope(
    owner: UUID5 | UUID7, fake_llm: FakeLLM, fake_embedder: RecordingEmbedder
) -> None:
    other = uuid5()

    async def probe() -> tuple[int, int]:
        await seed_communities(owner, [0, 0])
        await seed_communities(other, [0, 0])
        await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))
        await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({other}))
        await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({other}))
        raptor_claims = (
            select(Entity.Claim.id.count())
            .join(Entity.Content, Entity.Content.id == Entity.Claim.content_id)
            .where(Entity.Content.type == "raptor_summary")
        )
        async with dbutil.actor(owner) as session:
            first = (await session.exec(raptor_claims.where(Entity.Claim.scopes == [owner]))).one()
        async with dbutil.actor(other) as session:
            second = (
                await session.exec(raptor_claims.where(Entity.Claim.scopes == [other]))
            ).one()
        return first, second

    first, second = dbutil.run(probe())
    assert first >= 2
    assert second >= 2


@pytest.mark.parametrize(
    ("axes", "expected"),
    [
        ([0, 0, 1, 2], 1),  # one merged pair mints a parent, two singletons carry up unchanged
        ([0, 1, 2, 3], 0),  # distinct axes never link, the first clustering merges nothing
        ([0], 0),  # fewer than two communities cannot cluster at all
    ],
)
def test_build_raptor_writes_the_expected_summary_count(
    owner: UUID5 | UUID7,
    fake_llm: FakeLLM,
    fake_embedder: RecordingEmbedder,
    axes: list[int],
    expected: int,
) -> None:
    async def probe() -> int:
        await seed_communities(owner, axes)
        return await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))

    assert dbutil.run(probe()) == expected


def test_build_raptor_bounds_fanout_and_child_text(
    owner: UUID5 | UUID7,
    fake_llm: FakeLLM,
    fake_embedder: RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aizk.graph.raptor.settings.raptor_branch_factor", 2)
    monkeypatch.setattr("aizk.graph.raptor.settings.raptor_build_concurrency", 2)
    monkeypatch.setattr("aizk.graph.raptor.settings.raptor_child_summary_chars", 10)

    async def probe() -> None:
        await seed_communities(owner, [0, 0, 0, 0, 0])
        await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))

    dbutil.run(probe())
    prompts = [
        call.messages[-1]["content"]
        for call in fake_llm.completions.calls
        if call.response_model.__name__ == "RaptorReport"
    ]
    assert len(prompts) == 2
    assert all(prompt.count("\n-") == 2 for prompt in prompts)
    assert all("covers" not in prompt for prompt in prompts)
