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
def test_vector_utilities_preserve_values_and_similarity_contracts(vector: list[float]) -> None:
    assert cosine(vector, vector) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    kept = Node(entity_id=uuid5(), label="theme", summary="a paragraph", embedding=[1.0, 0.0])
    parents = [(kept, [1.0, 0.0])]
    assert redundant_parent(parents, [0.99, 0.01], threshold=0.95) is kept
    assert redundant_parent(parents, [0.0, 1.0], threshold=0.95) is None
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


def test_build_raptor_lifts_communities_into_a_part_of_tree(
    owner: UUID5 | UUID7, fake_llm: FakeLLM, fake_embedder: RecordingEmbedder
) -> None:
    async def probe() -> tuple[int, int, int, list[tuple[int, int]], tuple[int, int]]:
        other = uuid5()
        await seed_communities(owner, [0, 0, 1, 1])
        await seed_communities(other, [0, 0])
        await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))
        written = await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))
        await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({other}))
        await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({other}))
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
        return written, leaves or 0, parents or 0, edges, (first or 0, second or 0)

    written, leaves, parents, edges, scoped = dbutil.run(probe())
    assert leaves == 4
    assert parents >= 1
    assert written == parents
    assert len(edges) == 4
    assert all(child == 0 and parent >= 1 for child, parent in edges)
    assert all(count >= 2 for count in scoped)


def test_two_themes_named_the_same_fold_into_one_leaf_instead_of_aborting(
    owner: UUID5 | UUID7, fake_llm: FakeLLM, fake_embedder: RecordingEmbedder
) -> None:
    """A summary entity is identified by its label, and `entity_id` folds case and spacing."""

    async def probe() -> list[tuple[str, str]]:
        async with dbutil.actor(owner) as session:
            session.add_all(
                (
                    Community(
                        id=uuid5(),
                        created_by=owner,
                        scopes=[owner],
                        label="Retrieval Research",
                        summary="the smaller theme",
                        embedding=basis(0),
                        member_ids=[uuid5()],
                    ),
                    Community(
                        id=uuid5(),
                        created_by=owner,
                        scopes=[owner],
                        label="retrieval  research",
                        summary="the bigger theme",
                        embedding=basis(1),
                        member_ids=[uuid5(), uuid5(), uuid5()],
                    ),
                    Community(
                        id=uuid5(),
                        created_by=owner,
                        scopes=[owner],
                        label="Vector Indexes",
                        summary="a separate theme",
                        embedding=basis(2),
                        member_ids=[uuid5(), uuid5()],
                    ),
                )
            )
        await build_raptor(fake_llm.llm, fake_embedder, scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            rows = await session.exec(
                text(
                    "SELECT ent.name, ec.attributes->>'summary' AS summary FROM entity_claim ec "
                    "JOIN entity_content ent ON ent.id = ec.content_id "
                    "WHERE ec.scopes = CAST(:scopes AS uuid[]) "
                    "AND ent.type = 'raptor_summary' "
                    "AND (ec.attributes->>'level')::int = 0"
                ),
                params={"scopes": [str(owner)]},
            )
            return [(row.name, row.summary) for row in rows]

    leaves = dbutil.run(probe())

    assert len(leaves) == 2
    assert ("retrieval  research", "the bigger theme") in leaves
    assert all(summary != "the smaller theme" for _, summary in leaves)

    # The staging itself folds the pair, so the database never sees the second claim and the
    # `on_conflict_do_nothing` behind it stays the belt rather than the braces.
    builder = RaptorBuilder(
        scopes=frozenset({owner}), llm=FakeLLM().llm, embed=RecordingEmbedder()
    )
    staged = builder.leaves(
        [
            Community(
                created_by=owner,
                scopes=[owner],
                label=label,
                summary=summary,
                embedding=basis(0),
                member_ids=[uuid5() for _ in range(size)],
            )
            for label, summary, size in (
                ("Retrieval Research", "the smaller theme", 1),
                ("retrieval  research", "the bigger theme", 3),
            )
        ]
    )
    assert len(staged) == len(builder.claims) == len(builder.contents) == 1
    assert staged[0].summary == "the bigger theme"


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
        if len(axes) == 1:
            builder = RaptorBuilder(
                scopes=frozenset({owner}), llm=FakeLLM().llm, embed=RecordingEmbedder()
            )
            assert await builder.similarity_groups([basis(axes[0])]) == [[0]]
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


def test_the_tree_never_claims_a_summary_is_part_of_itself_or_claims_a_pair_twice() -> None:
    """Folded labels can put a parent in its own member list and repeat one part-of pair."""
    builder = RaptorBuilder(
        scopes=frozenset({uuid5()}), llm=FakeLLM().llm, embed=RecordingEmbedder()
    )
    parent = Node(entity_id=uuid5(), label="Parent", summary="p", embedding=basis(0))
    child = Node(entity_id=uuid5(), label="Child", summary="c", embedding=basis(1))

    builder.connect([child, parent], parent)
    builder.connect([child], parent)

    assert [edge.subject_id for edge in builder.edges] == [child.entity_id]
    assert len(builder.edge_claims) == 1
