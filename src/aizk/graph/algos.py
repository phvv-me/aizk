import uuid
from typing import cast

import networkx as nx
from loguru import logger
from sqlalchemy import ARRAY, Uuid, func, literal, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..store import LiveFact


async def ppr_expand(
    session: AsyncSession,
    seed_entity_ids: list[uuid.UUID],
    top_n: int = 20,
) -> list[uuid.UUID]:
    """Expand seed entities to their associatively related entities by personalized pagerank.

    Walks the row-level-security-visible latest binary facts outward from the seeds with a bounded
    k-hop recursive CTE, capped at settings.ppr_max_hops hops and settings.ppr_max_fanout
    neighbors per node, then runs networkx personalized pagerank over that local subgraph,
    teleporting back to the seeds present in it with damping settings.ppr_alpha and returning the
    top_n highest-scoring non-seed entities — the HippoRAG signal, the entities a multi-hop walk
    keeps returning to. Bounding the walk in the database keeps memory and latency tied to the
    local neighborhood rather than the whole corpus. Reading `LiveFact` rather than the raw claim
    table keeps a superseded edge out of the walk with no separate visibility gate to hand-repeat.
    Returns [] for no seeds or none present in the graph.

    session: open, principal-scoped session whose visibility bounds the loaded graph.
    seed_entity_ids: entities to teleport back to, the matched graph seeds.
    top_n: number of related entities to return, excluding the seeds.
    """
    if not seed_entity_ids:
        return []
    # the visible edges, each capped to its first ppr_max_fanout objects by a per-subject row
    # number, so the recursive walk below never fans one hub entity out across the whole graph.
    ranked = (
        select(
            LiveFact.subject_id.label("subject_id"),
            LiveFact.object_id.label("object_id"),
            func.row_number()
            .over(partition_by=LiveFact.subject_id, order_by=LiveFact.object_id)
            .label("rank"),
        )
        .where(LiveFact.object_id.is_not(None))
        .subquery()
    )
    # the bounded walk: the seeds at depth zero, then each hop joins the capped edges onto the
    # current frontier until the hop depth reaches the cap, a union so a cycle terminates the walk.
    walk = select(
        func.unnest(sql_cast(seed_entity_ids, ARRAY(Uuid))).label("entity_id"),
        literal(0).label("depth"),
    ).cte("walk", recursive=True)
    walk = walk.union(
        select(ranked.c.object_id.label("entity_id"), (walk.c.depth + 1).label("depth"))
        .select_from(walk.join(ranked, ranked.c.subject_id == walk.c.entity_id))
        .where(walk.c.depth < settings.ppr_max_hops, ranked.c.rank <= settings.ppr_max_fanout)
    )
    node_ids = select(walk.c.entity_id)
    edges = await session.execute(
        select(LiveFact.subject_id, LiveFact.object_id).where(
            LiveFact.object_id.is_not(None),
            LiveFact.subject_id.in_(node_ids),
            LiveFact.object_id.in_(node_ids),
        )
    )
    graph = nx.DiGraph()
    graph.add_edges_from((row.subject_id, row.object_id) for row in edges)
    personalization = {seed: 1.0 for seed in seed_entity_ids if seed in graph}
    if not personalization:
        return []
    scores = nx.pagerank(graph, alpha=settings.ppr_alpha, personalization=personalization)
    seeds = set(seed_entity_ids)
    ranked_entities = sorted(
        (entity for entity in scores if entity not in seeds),
        key=lambda entity: scores[entity],
        reverse=True,
    )
    # networkx types its nodes as a generic hashable, so narrow back to the uuids we loaded in
    expanded = cast(list[uuid.UUID], ranked_entities[:top_n])
    logger.info(
        "ppr expanded {seeds} seeds to {count} related entities over {nodes} entities",
        seeds=len(seed_entity_ids),
        count=len(expanded),
        nodes=graph.number_of_nodes(),
    )
    return expanded
