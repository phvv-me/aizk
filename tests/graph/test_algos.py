import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import dbutil
import networkx as nx
import pytest
import seedgraph
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range

from aizk.config import settings
from aizk.graph.algos import ppr_expand
from aizk.store import LiveFact, acting_as

pytestmark = pytest.mark.usefixtures("migrated_db")


async def plant_graph(
    owner: uuid.UUID, node_count: int, edges: list[tuple[int, int]]
) -> list[uuid.UUID]:
    """Plant `node_count` entities and one binary `cites` fact per edge, return the node ids.

    owner: user that owns every planted content and claim.
    node_count: how many Concept entities to create.
    edges: directed (subject index, object index) pairs, each a fact linking two entities.
    """
    async with acting_as(owner) as session:
        nodes = [
            await seedgraph.add_entity(session, owner, f"Node {index}")
            for index in range(node_count)
        ]
        for subject, object_ in edges:
            await seedgraph.add_fact(
                session,
                owner,
                nodes[subject],
                statement=f"{subject} links {object_}",
                predicate="cites",
                object_id=nodes[object_],
            )
    return nodes


def reference_expansion(
    edges: list[tuple[uuid.UUID, uuid.UUID]], seeds: list[uuid.UUID], top_n: int
) -> set[uuid.UUID]:
    """The whole-graph personalized pagerank expansion, the in-memory oracle the walk meets.

    edges: the directed subject-to-object edges of the full graph.
    seeds: entities personalization teleports back to.
    top_n: how many non-seed entities to keep.
    """
    graph = nx.DiGraph()
    graph.add_edges_from(edges)
    scores = nx.pagerank(
        graph, alpha=settings.ppr_alpha, personalization={seed: 1.0 for seed in seeds}
    )
    seed_set = set(seeds)
    ranked = sorted(
        (entity for entity in scores if entity not in seed_set),
        key=lambda entity: scores[entity],
        reverse=True,
    )
    return {cast(uuid.UUID, entity) for entity in ranked[:top_n]}


def test_ppr_short_circuits_on_empty_and_isolated_seeds() -> None:
    """No seeds returns before any query, and a seed touching no edge ranks no degenerate graph."""

    async def body() -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        owner = await seedgraph.fresh_owner()
        async with acting_as(owner) as session:
            lonely = await seedgraph.add_entity(session, owner, "Lonely")
        async with acting_as(owner):
            return (
                await ppr_expand([], top_n=20),
                await ppr_expand([lonely], top_n=20),
            )

    empty, alone = dbutil.run(body())
    assert empty == []
    assert alone == []


def test_ppr_reaches_a_two_hop_neighbor_and_matches_the_unbounded_walk() -> None:
    """A seed-near-far chain reaches the two-hop entity, exactly the oracle's expansion on it.

    The far entity shares no direct edge with the seed, so its presence is the multi-hop reach a
    one-hop neighbor lookup misses, and on a graph inside the hop bound the bounded walk returns
    precisely the same related set the load-everything reference does.
    """

    async def body() -> tuple[set[uuid.UUID], set[uuid.UUID], uuid.UUID, uuid.UUID]:
        owner = await seedgraph.fresh_owner()
        nodes = await plant_graph(owner, 3, [(0, 1), (1, 2)])
        async with acting_as(owner):
            bounded = await ppr_expand([nodes[0]], top_n=20)
        reference = reference_expansion(
            [(nodes[0], nodes[1]), (nodes[1], nodes[2])], [nodes[0]], 20
        )
        return set(bounded), reference, nodes[1], nodes[2]

    bounded, reference, near, far = dbutil.run(body())
    assert near in bounded and far in bounded
    assert bounded == reference


def test_ppr_caps_depth_beyond_the_hop_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the hop cap at two, a four-link chain reaches the in-bound node but not the far one.

    The database-side recursive walk stops at the hop bound before the third link.
    """
    monkeypatch.setattr(settings, "ppr_max_hops", 2)

    async def body() -> tuple[set[uuid.UUID], uuid.UUID, uuid.UUID]:
        owner = await seedgraph.fresh_owner()
        nodes = await plant_graph(owner, 4, [(0, 1), (1, 2), (2, 3)])
        async with acting_as(owner):
            expanded = set(await ppr_expand([nodes[0]], top_n=20))
        return expanded, nodes[2], nodes[3]

    expanded, mid, beyond = dbutil.run(body())
    assert mid in expanded
    assert beyond not in expanded


def test_ppr_skips_a_superseded_edge() -> None:
    """A closed-`recorded` edge never joins the walk, since `ppr_expand` reads only `LiveFact`."""

    async def body() -> tuple[list[uuid.UUID], int]:
        owner = await seedgraph.fresh_owner()

        now = datetime.now(UTC)
        async with acting_as(owner) as session:
            seed = await seedgraph.add_entity(session, owner, "Seed")
            gone = await seedgraph.add_entity(session, owner, "Gone")
            await seedgraph.add_fact(
                session,
                owner,
                seed,
                statement="seed once linked gone",
                predicate="cites",
                object_id=gone,
                recorded=Range(now - timedelta(days=1), now),
            )
        async with acting_as(owner) as session:
            live = list(await session.scalars(select(LiveFact)))
            expanded = await ppr_expand([seed], top_n=20)
        return expanded, len(live)

    expanded, live_count = dbutil.run(body())
    assert live_count == 0  # the only edge is retired, so the live view admits none
    assert expanded == []  # and the walk finds no neighbor to rank
