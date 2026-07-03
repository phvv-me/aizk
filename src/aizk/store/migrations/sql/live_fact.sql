-- live_fact narrows the fact_claim x fact_content join to exactly the live version of each claim,
-- exposing both the claim's own id and the content_id it stakes so a consumer can tell "this
-- version of memory" from "this deduplicated structural edge". FactClaim.is_current's own
-- predicate, rendered once as a view rather than re-derived by hand at every read site.
--
-- security_invoker = true is load-bearing: a default (security_definer-like) view runs as the
-- view's owning role rather than the querying session and silently bypasses row level security, so
-- this stays hand-written DDL rather than ORM-generated; SQLAlchemy 2.1.0b3's CreateView compiler
-- carries no security_invoker path. Static, no backend branches, so it is plain SQL rather than a
-- Jinja template.
CREATE VIEW live_fact WITH (security_invoker = true) AS
SELECT
    claim.id AS id,
    claim.content_id AS content_id,
    content.subject_id AS subject_id,
    content.object_id AS object_id,
    content.predicate AS predicate,
    content.statement AS statement,
    content.embedding AS embedding,
    claim.owner_id AS owner_id,
    claim.scope AS scope,
    claim.valid AS valid,
    claim.recorded AS recorded,
    claim.reviewed_at AS reviewed_at,
    claim.last_accessed AS last_accessed,
    claim.access_count AS access_count,
    claim.attributes AS attributes,
    claim.source_chunk_id AS source_chunk_id,
    claim.promoted_from AS promoted_from
FROM fact_claim claim
JOIN fact_content content ON content.id = claim.content_id
WHERE upper_inf(claim.recorded) AND (claim.valid IS NULL OR claim.valid @> now())
