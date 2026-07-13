import uuid
from collections.abc import Iterator

import dbutil
import networkx as nx
import pytest
from factories import build_live_fact
from hypothesis import given
from hypothesis import strategies as st
from sqlmodel import select

import aizk.graph.communities as communities_module
from aizk.graph.communities import build_communities, detect
from aizk.store import (
    Community,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    LiveFact,
)

UNIT_VECTOR = [1.0] + [0.0] * 1023


@pytest.fixture
def owner(migrated_db: None) -> Iterator[uuid.UUID]:
    pid = uuid.uuid4()

    async def setup() -> None:
        await dbutil.reset_db()

    dbutil.run(setup())
    yield pid


def edge(subject: uuid.UUID, object_: uuid.UUID) -> LiveFact:
    return build_live_fact(subject_id=subject, object_id=object_)


@given(size=st.integers(min_value=3, max_value=6))
def test_detect_keeps_a_clique_and_drops_under_floor(size: int) -> None:
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
    facts = [build_live_fact(subject_id=uuid.uuid4(), object_id=None) for _ in range(3)]
    assert detect(facts, min_size=3) == []


@pytest.mark.parametrize("backend", ["networkx", "cugraph"])
def test_detect_forwards_the_backend_keyword_only_off_the_default(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    captured: dict[str, object] = {}

    def fake_louvain(graph: nx.Graph, seed: int, **kwargs: object) -> list[set[uuid.UUID]]:
        captured.update(kwargs)
        return [set(graph.nodes())]

    monkeypatch.setattr(communities_module, "louvain_communities", fake_louvain)
    nodes = [uuid.uuid4() for _ in range(3)]
    facts = [edge(a, b) for i, a in enumerate(nodes) for b in nodes[i + 1 :]]

    clusters = detect(facts, min_size=3, backend=backend)
    assert clusters == [set(nodes)]
    assert captured.get("backend") == (None if backend == "networkx" else backend)


@pytest.mark.usefixtures("fake_embedder")
def test_build_lands_an_embedded_community(owner: uuid.UUID, fake_llm: object) -> None:
    async def probe() -> tuple[int, list[Community]]:
        nodes = [uuid.uuid4() for _ in range(3)]
        async with dbutil.actor(owner) as session:
            for index, node in enumerate(nodes):
                session.add(
                    EntityContent(
                        id=node, name=f"node {index}", type="concept", embedding=UNIT_VECTOR
                    )
                )
            await session.flush()
            session.add_all(
                EntityClaim(content_id=node, created_by=owner, scopes=[owner]) for node in nodes
            )
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
                FactClaim(content_id=content.id, created_by=owner, scopes=[owner])
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
