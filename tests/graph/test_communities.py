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
from aizk.graph.communities import (
    CommunityBuilder,
    CommunityDetector,
    CommunityFact,
    build_communities,
)
from aizk.graph.models import CommunitySummary
from aizk.store import (
    Community,
    Entity,
    Fact,
)

UNIT_VECTOR = [1.0] + [0.0] * 1023


def detector(**overrides: float | int | str) -> CommunityDetector:
    """Build a detector from the configured defaults with the named fields replaced."""
    return CommunityDetector.from_settings(settings).model_copy(update=overrides)


def graph_facts(graph: nx.Graph) -> list[CommunityFact]:
    """Turn one synthetic graph's edges into the facts detection reads."""
    named = nx.relabel_nodes(graph, {node: uuid5() for node in graph.nodes})
    return [
        CommunityFact(
            subject_id=subject, object_id=object_, statement=f"{subject} meets {object_}"
        )
        for subject, object_ in named.edges
    ]


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

    kept = detector().detect(facts)
    assert all(len(cluster) >= 3 for cluster in kept)
    assert set().union(*kept) >= set(clique)
    assert all(member not in cluster for cluster in kept for member in pair)
    assert detector(min_size=size + 3).detect(facts) == []
    isolated = [LiveFactFactory.build(subject_id=uuid5(), object_id=None) for _ in range(3)]
    assert detector().detect(isolated) == []

    captured: dict[str, str] = {}

    def fake_louvain(graph: nx.Graph, **kwargs: str) -> list[set[UUID5 | UUID7]]:
        captured.update(kwargs)
        return [set(graph.nodes())]

    with monkeypatch.context() as patch:
        patch.setattr(communities_module, "louvain_communities", fake_louvain)
        clusters = detector(backend=backend, max_size=1).detect(facts)
    assert clusters == [set((*clique, *pair))]
    assert captured.get("backend") == (None if backend == "networkx" else backend)


def test_refinement_splits_the_blobs_one_modularity_pass_leaves_behind() -> None:
    facts = graph_facts(nx.barabasi_albert_graph(400, 2, seed=3))

    coarse = detector(max_size=10**6, max_depth=0).detect(facts)
    refined = detector().detect(facts)

    # One pass over a hub-shaped graph saturates, since modularity cannot see past its
    # resolution limit and answers with a handful of clusters far too big to summarize.
    assert max(len(cluster) for cluster in coarse) > settings.community_max_size
    assert len(refined) > len(coarse)
    # Every part of this graph is divisible, so every leaf lands under the bound here. That
    # is a property of the fixture rather than a promise the algorithm makes, and the star
    # below is the shape where it cannot hold.
    assert max(len(cluster) for cluster in refined) <= settings.community_max_size
    members = [member for cluster in refined for member in cluster]
    assert len(members) == len(set(members)) == 400


def test_a_hub_louvain_will_not_split_keeps_every_spoke_in_one_theme() -> None:
    """The honest contract, since a star has no partition modularity prefers to the whole."""
    facts = graph_facts(nx.star_graph(1000))

    clusters = detector().detect(facts)

    assert [len(cluster) for cluster in clusters] == [1001]


def test_default_settings_theme_a_few_hundred_entities_into_readable_groups() -> None:
    facts = graph_facts(nx.ring_of_cliques(40, 8))

    clusters = detector().detect(facts)

    assert 10 <= len(clusters) <= 80
    assert all(3 <= len(cluster) <= settings.community_max_size for cluster in clusters)
    assert sum(len(cluster) for cluster in clusters) == 320


def test_a_cluster_louvain_will_not_split_survives_the_size_bound() -> None:
    facts = graph_facts(nx.complete_graph(12))

    assert [len(cluster) for cluster in detector(max_size=8).detect(facts)] == [12]


class StubLLM:
    """A generation double that answers every call and refuses the first `failures` of them."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    async def generate(
        self, system: str, prompt: str, schema: type[CommunitySummary]
    ) -> CommunitySummary:
        del system, prompt
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("the model refused")
        return schema(label=f"theme {self.calls}", summary=f"summary {self.calls}")


def builder_over(
    owner: UUID5 | UUID7,
    members: list[list[UUID5 | UUID7]],
    stored: list[Community] | None = None,
) -> CommunityBuilder:
    """Build one builder over synthetic clusters, each member carrying its own name."""
    names = {member: f"entity {index}" for index, group in enumerate(members) for member in group}
    facts = [edge(group[0], group[-1]) for group in members]
    return CommunityBuilder(frozenset({owner}), names, facts, stored or [])


def stub_generation(monkeypatch: pytest.MonkeyPatch, failures: int = 0) -> StubLLM:
    """Point community summarization at one stub, returning it for call counting."""
    stub = StubLLM(failures=failures)
    monkeypatch.setattr(
        communities_module.LLM, "from_settings", classmethod(lambda cls, config: stub)
    )
    return stub


@pytest.mark.usefixtures("fake_embedder")
def test_a_theme_whose_members_did_not_move_is_carried_forward_unsummarized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid5()
    kept = [uuid5(), uuid5(), uuid5()]
    fresh = [uuid5(), uuid5(), uuid5()]
    stored = [
        Community(
            created_by=owner,
            scopes=[owner],
            label="Kept theme",
            summary="the paragraph written last week",
            embedding=UNIT_VECTOR,
            member_ids=list(kept),
        )
    ]
    stub = stub_generation(monkeypatch)

    rows = dbutil.run(builder_over(owner, [kept, fresh], stored).rows([set(kept), set(fresh)]))

    assert stub.calls == 1
    assert rows[0].summary == "the paragraph written last week"
    assert rows[0].embedding == UNIT_VECTOR
    assert rows[1].summary == "summary 1"


@pytest.mark.usefixtures("fake_embedder")
def test_a_refused_summary_degrades_to_its_own_roster_instead_of_losing_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "community_summary_failure_ratio", 0.5)
    owner = uuid5()
    clusters = [[uuid5(), uuid5(), uuid5()] for _ in range(2)]
    stub = stub_generation(monkeypatch, failures=1)

    rows = dbutil.run(builder_over(owner, clusters).rows([set(group) for group in clusters]))

    assert stub.calls == 2
    assert rows[0].summary.startswith("Unsummarized theme covering entity 0")
    assert rows[0].label.endswith("and 2 related subjects")
    assert rows[1].summary == "summary 2"


@pytest.mark.usefixtures("fake_embedder")
def test_a_run_that_lost_too_many_summaries_keeps_the_stored_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid5()
    clusters = [[uuid5(), uuid5(), uuid5()] for _ in range(4)]
    stub_generation(monkeypatch, failures=4)

    with pytest.raises(RuntimeError, match="4 of 4 community summaries failed"):
        dbutil.run(builder_over(owner, clusters).rows([set(group) for group in clusters]))


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
        nodes = [uuid5() for _ in range(20)]
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
