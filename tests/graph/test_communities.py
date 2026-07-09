import uuid
from collections.abc import Iterator

import dbutil
import networkx as nx
import pytest
from factories import build_live_fact
from hypothesis import given
from hypothesis import strategies as st

from aizk.graph.communities import build_communities, community_search, detect
from aizk.store import EntityClaim, EntityContent, FactClaim, FactContent, LiveFact, acting_as

UNIT_VECTOR = [1.0] + [0.0] * 1023


@pytest.fixture
def owner(migrated_db: None) -> Iterator[uuid.UUID]:
    """A freshly reset schema seeding one user, the owner every DB body acts as."""
    pid = uuid.uuid4()

    async def setup() -> None:
        await dbutil.reset_db()
        await dbutil.seed_user(pid)

    dbutil.run(setup())
    yield pid


def edge(subject: uuid.UUID, object_: uuid.UUID) -> LiveFact:
    """One in-memory binary fact carrying just the subject-to-object edge `detect` reads."""
    return build_live_fact(subject_id=subject, object_id=object_)


@given(size=st.integers(min_value=3, max_value=6))
def test_detect_keeps_a_clique_and_drops_under_floor(size: int) -> None:
    """A clique above the floor lands in some kept community while a disjoint pair never does.

    Also raising the floor past every component's size leaves nothing, so the same seeded graph
    exercises the min-size filter's keep side and its drop-everything side in one property.
    """
    clique = [uuid.uuid4() for _ in range(size)]
    pair = [uuid.uuid4(), uuid.uuid4()]
    facts = [edge(a, b) for i, a in enumerate(clique) for b in clique[i + 1 :]]
    facts.append(edge(*pair))

    kept = detect(facts, min_size=3)
    assert all(len(cluster) >= 3 for cluster in kept)
    assert set().union(*kept) >= set(clique)
    assert all(member not in cluster for cluster in kept for member in pair)
    assert detect(facts, min_size=size + 3) == []


def test_detect_returns_nothing_without_edges() -> None:
    """Unary facts carry no edge, so the graph has no edges and no community is detected."""
    facts = [build_live_fact(subject_id=uuid.uuid4(), object_id=None) for _ in range(3)]
    assert detect(facts, min_size=3) == []


@pytest.mark.parametrize("backend", ["networkx", "cugraph"])
def test_detect_forwards_the_backend_keyword_only_off_the_default(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """The accelerator name reaches Louvain as a keyword while the in-process default omits it.

    Whether cugraph partitions on a GPU is networkx's contract, not ours, so Louvain is stubbed
    and only our own dispatch is pinned: the backend keyword rides along when an accelerator is
    named and is left off entirely for plain networkx, the one seam a GPU tier flips.
    """
    captured: dict[str, object] = {}

    def fake_louvain(graph: nx.Graph, seed: int, **kwargs: object) -> list[set[uuid.UUID]]:
        captured.update(kwargs)
        return [set(graph.nodes())]

    monkeypatch.setattr(nx.community, "louvain_communities", fake_louvain)
    nodes = [uuid.uuid4() for _ in range(3)]
    facts = [edge(a, b) for i, a in enumerate(nodes) for b in nodes[i + 1 :]]

    clusters = detect(facts, min_size=3, backend=backend)
    assert clusters == [set(nodes)]
    assert captured.get("backend") == (None if backend == "networkx" else backend)


@pytest.mark.usefixtures("fake_embedder")
def test_build_then_search_lands_a_searchable_community(
    owner: uuid.UUID, fake_llm: object
) -> None:
    """A triangle of facts becomes one stored community that a thematic search then surfaces.

    Detection, the fake LLM summary, the embed, and the row write all run for real off the fake
    seams, so a non-empty search result proves the whole build-and-rank path landed a row.
    """

    async def probe() -> tuple[int, list[tuple[str, str, float]]]:
        nodes = [uuid.uuid4() for _ in range(3)]
        async with acting_as(owner) as session:
            for index, node in enumerate(nodes):
                session.add(
                    EntityContent(
                        id=node, name=f"node {index}", type="Concept", embedding=UNIT_VECTOR
                    )
                )
            await session.flush()
            session.add_all(EntityClaim(content_id=node, owner_id=owner) for node in nodes)
            contents = [
                FactContent(
                    id=uuid.uuid4(),
                    subject_id=subject,
                    object_id=object_,
                    predicate="related_to",
                    statement=f"{subject} links {object_}",
                    embedding=UNIT_VECTOR,
                )
                for i, subject in enumerate(nodes)
                for object_ in nodes[i + 1 :]
            ]
            session.add_all(contents)
            await session.flush()
            session.add_all(
                FactClaim(content_id=content.id, owner_id=owner) for content in contents
            )
        written = await build_communities(user_id=owner)
        async with acting_as(owner):
            found = await community_search(UNIT_VECTOR, k=3)
        return written, found

    written, found = dbutil.run(probe())
    assert written >= 1
    assert len(found) >= 1
    label, summary, score = found[0]
    assert label and summary
