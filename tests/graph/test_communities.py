from collections.abc import Iterator

import dbutil
import networkx as nx
import pytest
from doubles import FakeLLM
from factories import LiveFactFactory
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5
from pydantic import UUID5, UUID7
from sqlmodel import select

import aizk.graph.communities as communities_module
from aizk.config import settings
from aizk.graph.communities import CommunityBuilder, build_communities, detect
from aizk.store import (
    Community,
    Entity,
    Fact,
)

UNIT_VECTOR = [1.0] + [0.0] * 1023


@pytest.fixture
def owner(migrated_db: None) -> Iterator[UUID5 | UUID7]:
    pid = uuid5()

    async def setup() -> None:
        await dbutil.reset_db()

    dbutil.run(setup())
    yield pid


def edge(subject: UUID5 | UUID7, object_: UUID5 | UUID7) -> Fact.Live:
    return LiveFactFactory.build(subject_id=subject, object_id=object_)


@pytest.mark.parametrize("backend", ["networkx", "cugraph"])
@given(size=st.integers(min_value=3, max_value=6))
def test_detect_filters_small_clusters_and_forwards_nondefault_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    size: int,
) -> None:
    clique = [uuid5() for _ in range(size)]
    pair = [uuid5(), uuid5()]
    facts = [edge(a, b) for i, a in enumerate(clique) for b in clique[i + 1 :]]
    facts.append(edge(*pair))

    kept = detect(facts, min_size=3)
    assert all(len(cluster) >= 3 for cluster in kept)
    assert set().union(*kept) >= set(clique)
    assert all(member not in cluster for cluster in kept for member in pair)
    assert detect(facts, min_size=size + 3) == []
    isolated = [LiveFactFactory.build(subject_id=uuid5(), object_id=None) for _ in range(3)]
    assert detect(isolated, min_size=3) == []

    captured: dict[str, str] = {}

    def fake_louvain(graph: nx.Graph, seed: int, **kwargs: str) -> list[set[UUID5 | UUID7]]:
        captured.update(kwargs)
        return [set(graph.nodes())]

    with monkeypatch.context() as patch:
        patch.setattr(communities_module, "louvain_communities", fake_louvain)
        clusters = detect(facts, min_size=3, backend=backend)
    assert clusters == [set((*clique, *pair))]
    assert captured.get("backend") == (None if backend == "networkx" else backend)


def test_prompt_bounds_and_deduplicates_the_cluster_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha, beta = uuid5(), uuid5()
    monkeypatch.setattr(settings, "community_entities_k", 1)
    monkeypatch.setattr(settings, "community_facts_k", 1)
    builder = CommunityBuilder(
        frozenset({uuid5()}),
        {
            alpha: "beta",
            beta: "alpha",
        },
        [
            LiveFactFactory.build(subject_id=alpha, object_id=beta, statement="new fact"),
            LiveFactFactory.build(subject_id=alpha, object_id=beta, statement="new fact"),
            LiveFactFactory.build(subject_id=alpha, object_id=beta, statement="old fact"),
        ],
    )

    assert builder.prompt({alpha, beta}) == "Entities: alpha\n\nFacts:\n- new fact"


@pytest.mark.usefixtures("fake_embedder")
def test_build_lands_an_embedded_community(owner: UUID5 | UUID7, fake_llm: FakeLLM) -> None:
    async def probe() -> tuple[int, list[Community]]:
        nodes = [uuid5() for _ in range(3)]
        async with dbutil.actor(owner) as session:
            for index, node in enumerate(nodes):
                session.add(
                    Entity.Content(
                        id=node, name=f"node {index}", type="concept", embedding=UNIT_VECTOR
                    )
                )
            await session.flush()
            session.add_all(
                Entity.Claim(content_id=node, created_by=owner, scopes=[owner]) for node in nodes
            )
            contents = [
                Fact.Content(
                    id=uuid5(),
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
                Fact.Claim(content_id=content.id, created_by=owner, scopes=[owner])
                for content in contents
            )
        written = await build_communities(scopes=frozenset({owner}))
        async with dbutil.actor(owner) as session:
            found = list(await session.exec(select(Community)))
        return written, found

    written, found = dbutil.run(probe())
    assert written >= 1
    assert len(found) >= 1
    assert found[0].label and found[0].summary and found[0].embedding is not None
