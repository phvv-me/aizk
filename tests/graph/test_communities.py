import asyncio
import uuid

import networkx as nx
import pytest
from factories import build_live_fact
from graphdb import FakeLLM
from hypothesis import given
from hypothesis import strategies as st

from aizk.graph.communities import build_communities, community_search, detect
from aizk.store import EntityClaim, EntityContent, FactClaim, FactContent, LiveFact, acting_as

UNIT_VECTOR = [1.0] + [0.0] * 1023


def edge(subject: uuid.UUID, object_: uuid.UUID) -> LiveFact:
    """An in-memory binary fact carrying just the subject-to-object edge `detect` reads.

    subject: entity the edge starts from.
    object_: entity the edge points to.
    """
    return build_live_fact(subject_id=subject, object_id=object_)


@given(size=st.integers(min_value=3, max_value=6))
def test_detect_keeps_a_clique_and_drops_a_smaller_component(size: int) -> None:
    """A clique above the floor forms one community while a disjoint under-floor pair is dropped.

    The detector's own job is the edge build and the min-size filter over whatever Louvain returns,
    so the clique members all land in some kept community and the loose pair never does.
    """
    clique = [uuid.uuid4() for _ in range(size)]
    pair = [uuid.uuid4(), uuid.uuid4()]
    facts = [edge(a, b) for i, a in enumerate(clique) for b in clique[i + 1 :]]
    facts.append(edge(pair[0], pair[1]))

    clusters = detect(facts, min_size=3)

    assert all(len(cluster) >= 3 for cluster in clusters)
    assert set().union(*clusters) >= set(clique)
    assert all(member not in cluster for cluster in clusters for member in pair)


@given(size=st.integers(min_value=2, max_value=5))
def test_detect_drops_everything_below_the_min_size(size: int) -> None:
    """Raising the floor above the only component's size leaves no community to summarize."""
    nodes = [uuid.uuid4() for _ in range(size)]
    facts = [edge(a, b) for i, a in enumerate(nodes) for b in nodes[i + 1 :]]
    assert detect(facts, min_size=size + 1) == []


def test_detect_returns_nothing_without_edges() -> None:
    """Unary facts carry no edge, so an edgeless graph yields no communities."""
    facts = [build_live_fact(subject_id=uuid.uuid4(), object_id=None) for _ in range(3)]
    assert detect(facts, min_size=3) == []


@pytest.mark.parametrize("backend", ["networkx", "cugraph"])
def test_detect_forwards_the_backend_keyword_only_off_the_default(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """The accelerator name reaches Louvain as a keyword, while the in-process default omits it.

    Louvain itself is stubbed since whether cugraph partitions on a GPU is networkx's contract, not
    ours, so the test pins only our own dispatch: the backend keyword is passed when an accelerator
    is named and left off entirely for plain networkx, the seam a GPU tier flips with no edit.
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
    fresh_principal: uuid.UUID, fake_llm: FakeLLM
) -> None:
    """A triangle of facts becomes one stored community that a thematic search then surfaces.

    Detection, the LLM summary, the embed, and the row write all run for real off the fake seams,
    so a non-empty search result proves the whole build-and-rank path landed a row.
    """
    owner = fresh_principal

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
            # content and claim share no ORM relationship(), only a bare FK column, so the fact
            # content above must actually flush before the matching claims below are added.
            await session.flush()
            session.add_all(
                FactClaim(content_id=content.id, owner_id=owner) for content in contents
            )
        written = await build_communities(principal_id=owner)
        found = await community_search("the broad theme", principal_id=owner, k=3)
        return written, found

    written, found = asyncio.run(probe())
    assert written >= 1
    assert len(found) >= 1
    label, summary, score = found[0]
    assert label and summary
