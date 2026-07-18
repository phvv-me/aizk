from typing import TYPE_CHECKING

from sqlalchemy import (
    ColumnElement,
    Float,
    Integer,
    any_,
    bindparam,
    exists,
    func,
    literal,
    union_all,
)
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.sql.selectable import CTE
from sqlmodel import select
from sqlmodel.sql.expression import Select

from .tables import (
    Community,
    Document,
    EntityClaim,
    EntityContent,
    EntityKind,
    FactClaim,
    FactContent,
    RelationKind,
    RelationPolicy,
)
from .views import LiveFact

if TYPE_CHECKING:
    from ...retrieval.models.lane import QueryContext


def seed_mass_from(
    weight: ColumnElement[float], distance: ColumnElement[float]
) -> ColumnElement[float]:
    """PageRank seed mass shrinking smoothly with cosine distance, w / (1 + d)."""
    return weight / (1 + distance)


class Entity:
    """Entity ontology and persistence models under one stable namespace."""

    Kind = EntityKind
    Content = EntityContent
    Claim = EntityClaim

    @classmethod
    def catalog(cls, context: QueryContext) -> CTE:
        """Group live entities and their state facts by ontology type and exact scopes."""
        kind_distance = cls.Kind.embedding @ context.vector
        relevant_kinds = (
            select(cls.Kind.name, kind_distance.label("distance"))
            .where(cls.Kind.structural.is_(False), cls.Kind.embedding.is_not(None))
            .order_by(kind_distance)
            .limit(context.k)
            .cte("relevant_entity_kind")
        )
        declared = (
            select(
                cls.Content.id,
                cls.Content.name,
                cls.Content.type,
                cls.Claim.scopes,
            )
            .add_columns(cls.Claim.created_by)
            .join(cls.Claim, cls.Claim.content_id == cls.Content.id)
            .join(
                Document,
                (Document.subject_type == cls.Content.type)
                & (Document.title.lower() == cls.Content.name.lower())
                & (Document.scopes == cls.Claim.scopes),
            )
            .where(Document.is_active())
        )
        endpoints = union_all(
            select(
                LiveFact.subject_id.label("id"),
                LiveFact.scopes,
                LiveFact.created_by,
            ),
            select(
                LiveFact.object_id.label("id"),
                LiveFact.scopes,
                LiveFact.created_by,
            ).where(LiveFact.object_id.is_not(None)),
        ).cte("live_fact_endpoint")
        inferred = (
            select(
                cls.Content.id,
                cls.Content.name,
                cls.Content.type,
                endpoints.c.scopes,
            )
            .add_columns(endpoints.c.created_by)
            .join(endpoints, endpoints.c.id == cls.Content.id)
        )
        live = union_all(declared, inferred).cte("live_entity")
        unique = (
            select(
                live.c.id,
                live.c.name,
                live.c.type,
                live.c.scopes,
            )
            .add_columns(live.c.created_by)
            .ext(distinct_on(live.c.type, live.c.name, live.c.scopes))
            .order_by(live.c.type, live.c.name, live.c.scopes, live.c.id)
            .cte("unique_live_entity")
        )
        states = (
            select(
                LiveFact.subject_id,
                LiveFact.scopes,
                LiveFact.statement.f.string_agg(literal(" and ")).label("states"),
            )
            .join(RelationKind, RelationKind.name == LiveFact.predicate)
            .where(RelationKind.policy == RelationPolicy.state)
            .group_by(LiveFact.subject_id, LiveFact.scopes)
            .cte("entity_state")
        )
        entry = unique.c.name + func.coalesce(literal(" (") + states.c.states + literal(")"), "")
        rows = (
            select(
                unique.c.id,
                unique.c.created_by,
                unique.c.scopes,
                unique.c.type,
            )
            .add_columns(
                relevant_kinds.c.distance,
                func.row_number()
                .over(
                    partition_by=(unique.c.type, unique.c.scopes),
                    order_by=unique.c.name,
                )
                .label("rank"),
                func.string_agg(entry, literal(", "))
                .over(
                    partition_by=(unique.c.type, unique.c.scopes),
                    order_by=unique.c.name,
                    rows=(None, None),
                )
                .label("entries"),
            )
            .join(relevant_kinds, relevant_kinds.c.name == unique.c.type)
            .outerjoin(
                states,
                (states.c.subject_id == unique.c.id) & (states.c.scopes == unique.c.scopes),
            )
            .cte("entity_catalog_row")
        )
        return (
            select(
                rows.c.id,
                rows.c.created_by,
                rows.c.scopes,
                rows.c.type,
            )
            .add_columns(rows.c.distance, rows.c.rank, rows.c.entries)
            .where(rows.c.rank == 1)
            .cte("entity_catalog")
        )

    @classmethod
    def seed_mass(cls, dense_facts: CTE, context: QueryContext) -> CTE:
        """Personalized PageRank seeds summed per entity.

        Entities the query names carry decisive mass, exact lowered-name matches at full
        mention mass and, when enabled, trigram matches at similarity-scaled mass so a
        misspelled mention still seeds without outweighing an exact one. When any name
        matches, mass spreads from the mentions alone, the pure connection signal; dense
        entity and fact-endpoint seeds are only the fallback.
        """
        mentions = context.entities
        mention_mass = bindparam("graph_mention_mass", type_=Float)
        lowered = cls.Content.name.lower()
        exact_mentions = select(
            cls.Content.id.label("entity_id"),
            mention_mass.label("mass"),
        ).where(lowered == any_(mentions))
        if context.fuzzy:
            mention = func.unnest(mentions).table_valued("mention").render_derived()
            fuzzy_matches = (
                select(
                    cls.Content.id.label("entity_id"),
                    (mention_mass * func.similarity(lowered, mention.c.mention)).label("mass"),
                )
                .select_from(mention.join(cls.Content, lowered.bool_op("%")(mention.c.mention)))
                .where(lowered != mention.c.mention)
            )
            mention_entities = union_all(exact_mentions, fuzzy_matches).cte("mention_entity")
        else:
            mention_entities = exact_mentions.cte("mention_entity")
        entity_distance = cls.Content.embedding @ context.vector
        dense_entities = (
            select(
                cls.Content.id.label("entity_id"),
                seed_mass_from(
                    bindparam("graph_entity_seed_weight", type_=Float), entity_distance
                ).label("mass"),
            )
            .where(cls.Content.embedding.is_not(None))
            .order_by(entity_distance)
            .limit(bindparam("graph_seed_entities", type_=Integer))
        )
        endpoint_mass = seed_mass_from(
            bindparam("graph_fact_seed_weight", type_=Float), dense_facts.c.distance
        )
        fact_endpoints = union_all(*LiveFact.endpoints(dense_facts, endpoint_mass.label("mass")))
        fallback = union_all(dense_entities, fact_endpoints).subquery("fallback_seed")
        seeded = union_all(
            select(mention_entities.c.entity_id, mention_entities.c.mass),
            select(fallback.c.entity_id, fallback.c.mass).where(
                ~exists(select(mention_entities.c.entity_id))
            ),
        ).subquery("seeded")
        return (
            select(seeded.c.entity_id, func.sum(seeded.c.mass).label("mass"))
            .group_by(seeded.c.entity_id)
            .cte("seed_mass")
        )


class Knowledge:
    """Cross-model rollups over the caller-visible knowledge graph."""

    @classmethod
    def totals(cls) -> Select[tuple[int, int, int, int]]:
        """Count visible sources, findings, subjects, and themes in one row."""
        return select(
            Document.total().label("sources"),
            LiveFact.total().label("findings"),
            EntityClaim.total().label("subjects"),
            Community.total().label("themes"),
        )


class Fact:
    """Immutable fact content, scoped claims, and the current fact view."""

    Content = FactContent
    Claim = FactClaim
    Live = LiveFact


class Relation:
    """Relation ontology models and their coexistence policies."""

    Kind = RelationKind
    Policy = RelationPolicy
