import asyncio
import socket
import uuid
from typing import NamedTuple, cast
from urllib.parse import urlsplit

import networkx as nx
import pytest
from sqlalchemy import bindparam, text

from aizk.cli import migrate
from aizk.config import settings
from aizk.graph.algos import ppr_expand
from aizk.store import acting_as, async_session, system_session


def port_open(host: str | None, port: int | None, timeout: float = 0.5) -> bool:
    """Whether a TCP connection to host and port succeeds within timeout.

    host: target hostname, treated as unreachable when missing.
    port: target port, treated as unreachable when missing.
    timeout: connection deadline in seconds.
    """
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def db_reachable(database_url: str) -> bool:
    """Whether a Postgres DSN's host accepts connections.

    database_url: the asyncpg DSN whose authority is probed.
    """
    db = urlsplit(database_url)
    return port_open(db.hostname, db.port)


DB_UP = db_reachable(settings.database_url)


class WalkProbe(NamedTuple):
    """Outcome of one personalized pagerank walk over a planted three-node chain.

    expanded: the entity content ids ppr_expand reached from the seed.
    near: the entity directly linked to the seed.
    far: the entity reachable only through the near one, two hops out.
    """

    expanded: list[uuid.UUID]
    near: uuid.UUID
    far: uuid.UUID


async def provision(principal: uuid.UUID) -> None:
    """Migrate to head and seed the walking principal.

    principal: identity that owns the planted chain and runs the walk.
    """
    migrate()
    async with async_session()() as session, session.begin():
        await session.execute(text("INSERT INTO principal (id) VALUES (:p)"), {"p": principal})


async def plant_chain(
    owner: uuid.UUID,
    nodes: list[uuid.UUID],
    facts: list[uuid.UUID],
) -> None:
    """Plant a seed to near to far chain of entity content joined by two fact content rows.

    Each planted node is a content row plus this owner's own private claim on it, and each
    planted fact the same, the two-insert content/claim shape the raw SQL here mirrors since
    `Id.id`'s `default_factory=uuid.uuid7` is a client-side Python default, never a server-side
    one a bare INSERT without an id column would pick up.

    owner: principal that owns every planted claim.
    nodes: the seed, near, and far entity content ids in walk order.
    facts: the two fact content ids linking seed to near and near to far.
    """
    seed, near, far = nodes
    async with acting_as(owner) as session:
        for node, name in zip(nodes, ("Seed", "Near", "Far"), strict=True):
            await session.execute(
                text("INSERT INTO entity_content (id, name, type) VALUES (:id, :name, 'Concept')"),
                {"id": node, "name": f"{name} {node.hex[:8]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO entity_claim (id, content_id, owner_id, scope) "
                    "VALUES (:claim, :id, :owner, NULL)"
                ),
                {"claim": uuid.uuid4(), "id": node, "owner": owner},
            )
        for fact, subject, object_ in ((facts[0], seed, near), (facts[1], near, far)):
            await session.execute(
                text(
                    "INSERT INTO fact_content (id, subject_id, object_id, predicate, statement) "
                    "VALUES (:id, :subj, :obj, 'cites', :stmt)"
                ),
                {"id": fact, "subj": subject, "obj": object_, "stmt": "x links y"},
            )
            await session.execute(
                text(
                    "INSERT INTO fact_claim (id, content_id, owner_id, scope) "
                    "VALUES (:claim, :id, :owner, NULL)"
                ),
                {"claim": uuid.uuid4(), "id": fact, "owner": owner},
            )


async def cleanup(
    owner: uuid.UUID,
    nodes: list[uuid.UUID],
    facts: list[uuid.UUID],
) -> None:
    """Remove the planted claims, content, and principal, so a run leaves no residue.

    Claims are this owner's own rows and delete under the ordinary write policy; content carries
    no owner of its own and only a server-wide admin may delete it, so that half runs as the
    system principal, deleting fact content before entity content since fact content's own
    subject/object foreign keys point at it.

    owner: principal that owns the rows to delete.
    nodes: the entity content ids to delete once their claims and facts are gone.
    facts: the fact content ids to delete before their entities.
    """
    async with acting_as(owner) as session:
        await session.execute(
            text("DELETE FROM fact_claim WHERE content_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": facts},
        )
        await session.execute(
            text("DELETE FROM entity_claim WHERE content_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": nodes},
        )
    async with system_session() as session:
        await session.execute(
            text("DELETE FROM fact_content WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": facts},
        )
        await session.execute(
            text("DELETE FROM entity_content WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": nodes},
        )
    async with async_session()() as session, session.begin():
        await session.execute(text("DELETE FROM principal WHERE id = :p"), {"p": owner})


async def walk_probe() -> WalkProbe:
    """Plant a seed to near to far chain and expand the seed by personalized pagerank.

    The far entity is two hops from the seed and never directly linked, so its presence in the
    expansion is the multi-hop reach a one-hop neighbor lookup would miss. Cleanup runs in a
    finally block so a failed assertion leaves no rows behind.
    """
    owner = uuid.uuid4()
    nodes = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    facts = [uuid.uuid4(), uuid.uuid4()]
    await provision(owner)
    try:
        await plant_chain(owner, nodes, facts)
        async with acting_as(owner) as session:
            expanded = await ppr_expand(session, [nodes[0]], top_n=20)
        return WalkProbe(expanded=expanded, near=nodes[1], far=nodes[2])
    finally:
        await cleanup(owner, nodes, facts)


@pytest.mark.skipif(not DB_UP, reason="aizk postgres not reachable")
def test_ppr_reaches_a_multi_hop_neighbor() -> None:
    """A walk seeded at the chain head reaches the two-hop entity it shares no direct edge with."""
    result = asyncio.run(walk_probe())

    assert result.far in result.expanded
    assert result.near in result.expanded
    assert result.far not in {result.near}


async def plant_edges(
    owner: uuid.UUID,
    nodes: list[uuid.UUID],
    edges: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]],
) -> None:
    """Plant the entity content and one fact content per edge, the arbitrary-graph planter.

    Each content row is planted beside this owner's own claim on it, the same two-insert shape
    `plant_chain` uses.

    owner: principal that owns every planted claim.
    nodes: the entity content ids to create as Concept nodes.
    edges: the subject, object, and fact content id of each directed edge to plant.
    """
    async with acting_as(owner) as session:
        for node in nodes:
            await session.execute(
                text("INSERT INTO entity_content (id, name, type) VALUES (:id, :name, 'Concept')"),
                {"id": node, "name": f"Node {node.hex[:8]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO entity_claim (id, content_id, owner_id, scope) "
                    "VALUES (:claim, :id, :owner, NULL)"
                ),
                {"claim": uuid.uuid4(), "id": node, "owner": owner},
            )
        for fact, subject, object_ in edges:
            await session.execute(
                text(
                    "INSERT INTO fact_content (id, subject_id, object_id, predicate, statement) "
                    "VALUES (:id, :subj, :obj, 'cites', :stmt)"
                ),
                {"id": fact, "subj": subject, "obj": object_, "stmt": "x links y"},
            )
            await session.execute(
                text(
                    "INSERT INTO fact_claim (id, content_id, owner_id, scope) "
                    "VALUES (:claim, :id, :owner, NULL)"
                ),
                {"claim": uuid.uuid4(), "id": fact, "owner": owner},
            )


def unbounded_expand(
    edges: list[tuple[uuid.UUID, uuid.UUID]],
    seeds: list[uuid.UUID],
    top_n: int,
) -> set[uuid.UUID]:
    """The reference personalized pagerank expansion over the whole edge set, loaded in memory.

    Mirrors the old load-everything ppr_expand so a graph that fits inside the hop bound can be
    checked to yield the same related entities as the bounded walk.

    edges: the directed subject-to-object edges of the full graph.
    seeds: the entities personalization teleports back to.
    top_n: number of non-seed entities to keep.
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
    # networkx types its nodes as a generic hashable, so narrow back to the uuids we planted
    return {cast(uuid.UUID, entity) for entity in ranked[:top_n]}


class BoundProbe(NamedTuple):
    """Outcome of a bounded walk run beside its unbounded reference on the same small graph.

    bounded: the entities ppr_expand reached through the bounded recursive walk.
    reference: the entities the in-memory load-everything expansion reached.
    """

    bounded: set[uuid.UUID]
    reference: set[uuid.UUID]


async def within_bound_probe() -> BoundProbe:
    """Plant a small graph that fits inside the hop bound and expand it bounded and unbounded.

    The seed points to two entities that both point to a fourth, so every node sits within two
    hops of the seed and the bounded walk loads the identical subgraph the unbounded reference
    holds, which is the equality the bound must preserve.
    """
    owner = uuid.uuid4()
    seed, left, right, sink = (uuid.uuid4() for _ in range(4))
    nodes = [seed, left, right, sink]
    pairs = [(seed, left), (seed, right), (left, sink), (right, sink)]
    edges = [(uuid.uuid4(), subject, object_) for subject, object_ in pairs]
    fact_ids = [fact for fact, _, _ in edges]
    await provision(owner)
    try:
        await plant_edges(owner, nodes, edges)
        async with acting_as(owner) as session:
            bounded = await ppr_expand(session, [seed], top_n=20)
        reference = unbounded_expand(pairs, [seed], top_n=20)
        return BoundProbe(bounded=set(bounded), reference=reference)
    finally:
        await cleanup(owner, nodes, fact_ids)


async def depth_cap_probe() -> tuple[set[uuid.UUID], uuid.UUID, uuid.UUID]:
    """Plant a chain deeper than the hop bound, return the reach with the in- and out-bound nodes.

    With the hop cap pulled down to two, a four-link chain seeds a walk that reaches the first two
    links and must stop before the third, so the third node is the proof the depth cap holds.
    """
    owner = uuid.uuid4()
    seed, near, mid, beyond = (uuid.uuid4() for _ in range(4))
    nodes = [seed, near, mid, beyond]
    pairs = [(seed, near), (near, mid), (mid, beyond)]
    edges = [(uuid.uuid4(), subject, object_) for subject, object_ in pairs]
    fact_ids = [fact for fact, _, _ in edges]
    patch = pytest.MonkeyPatch()
    patch.setattr(settings, "ppr_max_hops", 2)
    try:
        await provision(owner)
        try:
            await plant_edges(owner, nodes, edges)
            async with acting_as(owner) as session:
                expanded = await ppr_expand(session, [seed], top_n=20)
            return set(expanded), mid, beyond
        finally:
            await cleanup(owner, nodes, fact_ids)
    finally:
        patch.undo()


@pytest.mark.skipif(not DB_UP, reason="aizk postgres not reachable")
def test_ppr_matches_the_unbounded_walk_within_the_hop_bound() -> None:
    """On a graph that fits inside the bound the bounded walk reaches the same related entities."""
    result = asyncio.run(within_bound_probe())

    assert result.bounded == result.reference
    assert len(result.bounded) == 3


@pytest.mark.skipif(not DB_UP, reason="aizk postgres not reachable")
def test_ppr_caps_depth_beyond_the_hop_bound() -> None:
    """A chain deeper than the hop cap reaches the in-bound node but never the out-of-bound one."""
    expanded, mid, beyond = asyncio.run(depth_cap_probe())

    assert mid in expanded
    assert beyond not in expanded


async def short_circuit_probe() -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Expand from no seeds and from one seed touching no edge, the two empty short-circuits.

    No seeds returns before any query, and a seed that reaches no edge builds an empty walk graph
    where personalization is empty, so both return nothing rather than ranking a degenerate graph.
    """
    owner = uuid.uuid4()
    isolated = uuid.uuid4()
    await provision(owner)
    try:
        async with acting_as(owner) as session:
            await session.execute(
                text(
                    "INSERT INTO entity_content (id, name, type) VALUES (:id, 'Lonely', 'Concept')"
                ),
                {"id": isolated},
            )
            await session.execute(
                text(
                    "INSERT INTO entity_claim (id, content_id, owner_id, scope) "
                    "VALUES (:claim, :id, :owner, NULL)"
                ),
                {"claim": uuid.uuid4(), "id": isolated, "owner": owner},
            )
            empty = await ppr_expand(session, [], top_n=20)
            alone = await ppr_expand(session, [isolated], top_n=20)
        return empty, alone
    finally:
        async with acting_as(owner) as session:
            await session.execute(
                text("DELETE FROM entity_claim WHERE content_id = :id"), {"id": isolated}
            )
        async with system_session() as session:
            await session.execute(
                text("DELETE FROM entity_content WHERE id = :id"), {"id": isolated}
            )
        async with async_session()() as session, session.begin():
            await session.execute(text("DELETE FROM principal WHERE id = :p"), {"p": owner})


@pytest.mark.skipif(not DB_UP, reason="aizk postgres not reachable")
def test_ppr_short_circuits_on_empty_and_isolated_seeds() -> None:
    """No seeds and a seed touching no edge both expand to nothing, never a degenerate ranking."""
    empty, alone = asyncio.run(short_circuit_probe())

    assert empty == []
    assert alone == []
