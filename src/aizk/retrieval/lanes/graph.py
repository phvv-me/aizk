from sqlalchemy import (
    ColumnElement,
    Float,
    Integer,
    any_,
    bindparam,
    exists,
    func,
    select,
    union_all,
)
from sqlalchemy.sql.selectable import CTE, Select

from ...store import EntityContent, LiveFact
from ..models.lane import QueryContext


def endpoint_selects(dense_facts: CTE, *extra: ColumnElement) -> tuple[Select, Select]:
    """One select per dense-fact endpoint, the object side guarded against nulls."""
    return (
        select(dense_facts.c.subject_id.label("entity_id"), *extra),
        select(dense_facts.c.object_id.label("entity_id"), *extra).where(
            dense_facts.c.object_id.is_not(None)
        ),
    )


def seed_mass_from(
    weight: ColumnElement[float], distance: ColumnElement[float]
) -> ColumnElement[float]:
    """PageRank seed mass shrinking smoothly with cosine distance, w / (1 + d)."""
    return weight / (1 + distance)


def multihop_part(dense_facts: CTE, context: QueryContext, hops: int) -> Select:
    """The personalized PageRank expansion as one fact part: mention-seeded mass diffuses
    a bounded number of degree-normalized hops and the weaker endpoint's accumulated mass
    orders each connecting fact."""
    seeds = seed_mass(dense_facts, context)
    mass = diffused_mass(seeds, hops)
    return connected_facts(mass)


def seed_mass(dense_facts: CTE, context: QueryContext) -> CTE:
    """Personalized PageRank seeds summed per entity.

    Entities the query names carry decisive mass, exact lowered-name matches at full
    mention mass and, when enabled, trigram matches at similarity-scaled mass so a
    misspelled mention still seeds without outweighing an exact one. When any name
    matches, mass spreads from the mentions alone, the pure connection signal; dense
    entity and fact-endpoint seeds are only the fallback.
    """
    mentions = context.entities
    mention_mass = bindparam("graph_mention_mass", type_=Float)
    lowered = func.lower(EntityContent.name)
    exact_mentions = select(
        EntityContent.id.label("entity_id"),
        mention_mass.label("mass"),
    ).where(lowered == any_(mentions))
    if context.fuzzy:
        mention = func.unnest(mentions).table_valued("mention").render_derived()
        fuzzy_matches = (
            select(
                EntityContent.id.label("entity_id"),
                (mention_mass * func.similarity(lowered, mention.c.mention)).label("mass"),
            )
            .select_from(mention.join(EntityContent, lowered.bool_op("%")(mention.c.mention)))
            .where(lowered != mention.c.mention)
        )
        mention_entities = union_all(exact_mentions, fuzzy_matches).cte("mention_entity")
    else:
        mention_entities = exact_mentions.cte("mention_entity")
    entity_distance = EntityContent.embedding @ context.vector
    dense_entities = (
        select(
            EntityContent.id.label("entity_id"),
            seed_mass_from(
                bindparam("graph_entity_seed_weight", type_=Float), entity_distance
            ).label("mass"),
        )
        .where(EntityContent.embedding.is_not(None))
        .order_by(entity_distance)
        .limit(bindparam("graph_seed_entities", type_=Integer))
    )
    endpoint_mass = seed_mass_from(
        bindparam("graph_fact_seed_weight", type_=Float), dense_facts.c.distance
    )
    fact_endpoints = union_all(*endpoint_selects(dense_facts, endpoint_mass.label("mass")))
    fallback = union_all(dense_entities, fact_endpoints).subquery("fallback_seed")
    seeded = union_all(
        select(mention_entities),
        select(fallback).where(~exists(select(mention_entities.c.entity_id))),
    ).subquery("seeded")
    return (
        select(seeded.c.entity_id, func.sum(seeded.c.mass).label("mass"))
        .group_by(seeded.c.entity_id)
        .cte("seed_mass")
    )


def diffused_mass(seeds: CTE, ppr_hops: int) -> CTE:
    """The seed mass diffused one bounded degree-normalized hop at a time, accumulated
    over every hop and cut to the mass window.

    Each direction joins the frontier through its own endpoint index instead of a
    membership test over every fact.
    """
    ppr_frontier = bindparam("graph_ppr_frontier", type_=Integer)
    ppr_damping = bindparam("graph_ppr_damping", type_=Float)
    spread = [seeds]
    previous = seeds
    for hop in range(1, ppr_hops + 1):
        frontier = (
            select(previous.c.entity_id, previous.c.mass)
            .order_by(previous.c.mass.desc())
            .limit(ppr_frontier)
            .cte(f"frontier_{hop}")
        )
        edges = union_all(
            select(LiveFact.subject_id.label("src"), LiveFact.object_id.label("dst"))
            .join(frontier, LiveFact.subject_id == frontier.c.entity_id)
            .where(LiveFact.object_id.is_not(None)),
            select(LiveFact.object_id, LiveFact.subject_id).join(
                frontier, LiveFact.object_id == frontier.c.entity_id
            ),
        ).cte(f"edge_{hop}")
        degree = (
            select(edges.c.src, func.count().label("edges"))
            .group_by(edges.c.src)
            .subquery(f"degree_{hop}")
        )
        flow = func.sum(frontier.c.mass * ppr_damping / func.greatest(degree.c.edges, 1))
        previous = (
            select(edges.c.dst.label("entity_id"), flow.label("mass"))
            .select_from(
                edges.join(frontier, frontier.c.entity_id == edges.c.src).join(
                    degree, degree.c.src == edges.c.src
                )
            )
            .group_by(edges.c.dst)
            .cte(f"hop_{hop}")
        )
        spread.append(previous)
    accumulated = union_all(*(select(step) for step in spread)).subquery("spread")
    return (
        select(accumulated.c.entity_id, func.sum(accumulated.c.mass).label("mass"))
        .group_by(accumulated.c.entity_id)
        .order_by(func.sum(accumulated.c.mass).desc())
        .limit(bindparam("graph_mass_window", type_=Integer))
        .cte("entity_mass")
    )


def connected_facts(mass: CTE) -> Select:
    """The facts the accumulated mass connects, ordered by the weaker endpoint's mass.

    A connecting fact needs standing at both endpoints, so the score takes the weaker
    endpoint's mass, which lets a semantically distant hop outrank dense near-duplicates
    that merely touch one popular entity. Semantic order needs no second vote here, the
    dense part of the merged lane already casts it.
    """
    subject_mass = mass.alias("subject_mass")
    object_mass = mass.alias("object_mass")
    connection = func.least(
        subject_mass.c.mass,
        func.coalesce(
            object_mass.c.mass,
            subject_mass.c.mass * bindparam("graph_dangling_factor", type_=Float),
        ),
    )
    return (
        select(LiveFact.id, (-connection).label("ordering"))
        .join(subject_mass, subject_mass.c.entity_id == LiveFact.subject_id)
        .outerjoin(object_mass, object_mass.c.entity_id == LiveFact.object_id)
        .where(LiveFact.embedding.is_not(None))
        .order_by(connection.desc())
        .limit(bindparam("graph_facts_k", type_=Integer))
    )
