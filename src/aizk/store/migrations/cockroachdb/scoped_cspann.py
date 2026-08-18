# Give CockroachDB one scope-prefixed vector projection that remains behind database authority.

from aizk.config import settings
from alembic import op

_DIRECT_SOURCES = {
    "chunk": "chunk",
    "community": "community",
    "profile": "profile",
    "session_item": "session_item",
}
_KINDS = (*_DIRECT_SOURCES.values(), "entity", "fact")


def authority(permission: str, *, definer_safe: bool) -> str:
    """Render one scope array through the carrier visible at this privilege boundary."""
    if not definer_safe:
        return f"nullif(current_setting('app.scopes.{permission}', true), '')"
    positions = {"read": 2, "public": 4}
    return (
        "nullif(split_part(current_setting('application_name', true), '|', "
        f"{positions[permission]}), '')"
    )


def scope_function(*, definer_safe: bool = True, replace: bool = False) -> str:
    """Build the capability that lists exact visible scope partitions."""
    create = "CREATE OR REPLACE" if replace else "CREATE"
    read = authority("read", definer_safe=definer_safe)
    public = authority("public", definer_safe=definer_safe)
    return f"""
{create} FUNCTION aizk_private.cspann_scopes(requested_kind STRING)
RETURNS TABLE (scopes UUID[])
LANGUAGE SQL
SECURITY DEFINER
AS $$
SELECT DISTINCT candidate.scopes
FROM aizk_private.scoped_vector AS candidate
WHERE candidate.kind = requested_kind
  AND cardinality(candidate.scopes) > 0
  AND (
      candidate.scopes <@ CAST(
          {read} AS UUID[]
      )
      OR (
          cardinality(candidate.scopes) = 1
          AND candidate.scopes <@ CAST(
              {public} AS UUID[]
          )
      )
  )
$$
"""


def search_function(
    dimensions: int,
    *,
    definer_safe: bool = True,
    replace: bool = False,
) -> str:
    """Build the one capability routine allowed to read the private projection."""
    kinds = ", ".join(f"'{kind}'" for kind in _KINDS)
    create = "CREATE OR REPLACE" if replace else "CREATE"
    read = authority("read", definer_safe=definer_safe)
    public = authority("public", definer_safe=definer_safe)
    return f"""
{create} FUNCTION aizk_private.cspann_search(
    requested_kind STRING,
    requested_scopes UUID[],
    query_vector VECTOR({dimensions}),
    requested_limit INT8
)
RETURNS TABLE (source_id UUID, distance FLOAT8)
LANGUAGE PLpgSQL
SECURITY DEFINER
AS $$
BEGIN
    IF requested_kind NOT IN ({kinds}) THEN
        RAISE EXCEPTION 'unsupported vector projection kind' USING ERRCODE = '22023';
    END IF;
    IF requested_limit < 1 OR requested_limit > 4096 THEN
        RAISE EXCEPTION 'vector candidate limit is outside 1 through 4096'
            USING ERRCODE = '22023';
    END IF;
    IF NOT coalesce(
        cardinality(requested_scopes) > 0 AND (
            requested_scopes <@ CAST(
                {read} AS UUID[]
            )
            OR (
                cardinality(requested_scopes) = 1
                AND requested_scopes <@ CAST(
                    {public} AS UUID[]
                )
            )
        ),
        false
    ) THEN
        RAISE EXCEPTION 'unauthorized vector scope set' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    SELECT candidate.source_id, candidate.embedding <=> query_vector
    FROM aizk_private.scoped_vector AS candidate
    WHERE candidate.kind = requested_kind
      AND candidate.scopes = requested_scopes
    ORDER BY candidate.embedding <=> query_vector
    LIMIT requested_limit;
END
$$
"""


def vector_write_function(dimensions: int) -> str:
    """Build the trigger-only definer helper that writes one supplied projection row."""
    kinds = ", ".join(f"'{kind}'" for kind in _KINDS)
    return f"""
CREATE FUNCTION aizk_private.write_vector(
    requested_kind STRING,
    requested_source_id UUID,
    requested_content_id UUID,
    requested_scopes UUID[],
    requested_embedding VECTOR({dimensions}),
    keep_row BOOL
)
RETURNS BOOL
LANGUAGE PLpgSQL
SECURITY DEFINER
AS $$
BEGIN
    IF pg_trigger_depth() < 1 THEN
        RAISE EXCEPTION 'vector projection writes require a trigger' USING ERRCODE = '42501';
    END IF;
    IF requested_kind NOT IN ({kinds}) THEN
        RAISE EXCEPTION 'unsupported vector projection kind' USING ERRCODE = '22023';
    END IF;
    DELETE FROM aizk_private.scoped_vector
    WHERE kind = requested_kind AND source_id = requested_source_id;
    IF keep_row AND requested_embedding IS NOT NULL THEN
        INSERT INTO aizk_private.scoped_vector
            (kind, source_id, content_id, scopes, embedding)
        VALUES (
            requested_kind,
            requested_source_id,
            requested_content_id,
            requested_scopes,
            requested_embedding
        );
    END IF;
    RETURN true;
END
$$
"""


def content_write_function(dimensions: int) -> str:
    """Build the trigger-only definer helper that refreshes canonical content claims."""
    return f"""
CREATE FUNCTION aizk_private.write_content_vectors(
    requested_kind STRING,
    requested_content_id UUID,
    requested_embedding VECTOR({dimensions})
)
RETURNS BOOL
LANGUAGE PLpgSQL
SECURITY DEFINER
AS $$
BEGIN
    IF pg_trigger_depth() < 1 THEN
        RAISE EXCEPTION 'vector projection writes require a trigger' USING ERRCODE = '42501';
    END IF;
    DELETE FROM aizk_private.scoped_vector
    WHERE kind = requested_kind AND content_id = requested_content_id;
    IF requested_embedding IS NULL THEN
        RETURN true;
    END IF;
    IF requested_kind = 'entity' THEN
        INSERT INTO aizk_private.scoped_vector
            (kind, source_id, content_id, scopes, embedding)
        SELECT 'entity', claim.id, requested_content_id, claim.scopes, requested_embedding
        FROM public.entity_claim AS claim
        WHERE claim.content_id = requested_content_id;
    ELSIF requested_kind = 'fact' THEN
        INSERT INTO aizk_private.scoped_vector
            (kind, source_id, content_id, scopes, embedding)
        SELECT 'fact', claim.id, requested_content_id, claim.scopes, requested_embedding
        FROM public.fact_claim AS claim
        WHERE claim.content_id = requested_content_id
          AND claim.recorded_to IS NULL
          AND (claim.valid_from IS NULL OR claim.valid_from <= now())
          AND (claim.valid_to IS NULL OR claim.valid_to > now());
    ELSE
        RAISE EXCEPTION 'unsupported vector content kind' USING ERRCODE = '22023';
    END IF;
    RETURN true;
END
$$
"""


def direct_trigger_function() -> str:
    """Build the direct-source trigger that delegates its row to the checked helper."""
    return """
CREATE FUNCTION aizk_private.sync_direct_vector()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    synchronized BOOL;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT aizk_private.write_vector(
            TG_ARGV[0], (OLD).id, (OLD).id, (OLD).scopes, NULL, false
        ) INTO synchronized;
        RETURN OLD;
    END IF;
    SELECT aizk_private.write_vector(
        TG_ARGV[0], (NEW).id, (NEW).id, (NEW).scopes, (NEW).embedding, true
    ) INTO synchronized;
    RETURN NEW;
END
$$
"""


def entity_trigger_function() -> str:
    """Build the entity-claim trigger that copies its canonical embedding."""
    return """
CREATE FUNCTION aizk_private.sync_entity_vector()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    content_embedding VECTOR;
    synchronized BOOL;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT aizk_private.write_vector(
            'entity', (OLD).id, (OLD).content_id, (OLD).scopes, NULL, false
        ) INTO synchronized;
        RETURN OLD;
    END IF;
    SELECT content.embedding INTO content_embedding
    FROM public.entity_content AS content
    WHERE content.id = (NEW).content_id;
    SELECT aizk_private.write_vector(
        'entity', (NEW).id, (NEW).content_id, (NEW).scopes, content_embedding, true
    ) INTO synchronized;
    RETURN NEW;
END
$$
"""


def fact_trigger_function() -> str:
    """Build the fact-claim trigger that excludes claims outside their live interval."""
    return """
CREATE FUNCTION aizk_private.sync_fact_vector()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    content_embedding VECTOR;
    keep_row BOOL;
    synchronized BOOL;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT aizk_private.write_vector(
            'fact', (OLD).id, (OLD).content_id, (OLD).scopes, NULL, false
        ) INTO synchronized;
        RETURN OLD;
    END IF;
    SELECT content.embedding INTO content_embedding
    FROM public.fact_content AS content
    WHERE content.id = (NEW).content_id;
    keep_row := (NEW).recorded_to IS NULL
        AND ((NEW).valid_from IS NULL OR (NEW).valid_from <= now())
        AND ((NEW).valid_to IS NULL OR (NEW).valid_to > now());
    SELECT aizk_private.write_vector(
        'fact', (NEW).id, (NEW).content_id, (NEW).scopes, content_embedding, keep_row
    ) INTO synchronized;
    RETURN NEW;
END
$$
"""


def content_trigger_function() -> str:
    """Build the canonical-content trigger that delegates all existing claims."""
    return """
CREATE FUNCTION aizk_private.refresh_content_vectors()
RETURNS TRIGGER
LANGUAGE PLpgSQL
AS $$
DECLARE
    synchronized BOOL;
BEGIN
    SELECT aizk_private.write_content_vectors(TG_ARGV[0], (NEW).id, (NEW).embedding)
    INTO synchronized;
    RETURN NEW;
END
$$
"""


def create() -> None:
    """Build a private, trigger-maintained, exact-scope C-SPANN capability."""
    dimensions = settings.embed_dim
    connection = op.get_bind()
    app_role = connection.dialect.identifier_preparer.quote(settings.app_role)
    op.execute("CREATE SCHEMA IF NOT EXISTS aizk_private")
    op.execute("REVOKE ALL ON SCHEMA aizk_private FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA aizk_private TO {app_role}")
    op.execute(
        f"""
CREATE TABLE aizk_private.scoped_vector (
    kind STRING NOT NULL,
    source_id UUID NOT NULL,
    content_id UUID NOT NULL,
    scopes UUID[] NOT NULL,
    embedding VECTOR({dimensions}) NOT NULL,
    CONSTRAINT scoped_vector_pkey PRIMARY KEY (kind, source_id)
)
"""
    )
    op.execute(
        "CREATE INDEX ix_scoped_vector_content ON aizk_private.scoped_vector (kind, content_id)"
    )
    op.execute("CREATE INDEX ix_scoped_vector_scope ON aizk_private.scoped_vector (kind, scopes)")
    for table, kind in _DIRECT_SOURCES.items():
        op.execute(
            "INSERT INTO aizk_private.scoped_vector "
            "(kind, source_id, content_id, scopes, embedding) "
            f"SELECT '{kind}', id, id, scopes, embedding FROM public.{table} "
            "WHERE embedding IS NOT NULL"
        )
    op.execute(
        "INSERT INTO aizk_private.scoped_vector "
        "(kind, source_id, content_id, scopes, embedding) "
        "SELECT 'entity', claim.id, content.id, claim.scopes, content.embedding "
        "FROM public.entity_claim AS claim "
        "JOIN public.entity_content AS content ON content.id = claim.content_id "
        "WHERE content.embedding IS NOT NULL"
    )
    op.execute(
        "INSERT INTO aizk_private.scoped_vector "
        "(kind, source_id, content_id, scopes, embedding) "
        "SELECT 'fact', claim.id, content.id, claim.scopes, content.embedding "
        "FROM public.fact_claim AS claim "
        "JOIN public.fact_content AS content ON content.id = claim.content_id "
        "WHERE content.embedding IS NOT NULL AND claim.recorded_to IS NULL "
        "AND (claim.valid_from IS NULL OR claim.valid_from <= now()) "
        "AND (claim.valid_to IS NULL OR claim.valid_to > now())"
    )
    op.execute(
        "CREATE VECTOR INDEX ix_scoped_vector_embedding "
        "ON aizk_private.scoped_vector (kind, scopes, embedding vector_cosine_ops)"
    )
    op.execute(scope_function())
    op.execute(search_function(dimensions))
    op.execute(vector_write_function(dimensions))
    op.execute(content_write_function(dimensions))
    op.execute(direct_trigger_function())
    op.execute(entity_trigger_function())
    op.execute(fact_trigger_function())
    op.execute(content_trigger_function())
    definers = (
        "cspann_scopes(STRING)",
        f"cspann_search(STRING, UUID[], VECTOR({dimensions}), INT8)",
        f"write_vector(STRING, UUID, UUID, UUID[], VECTOR({dimensions}), BOOL)",
        f"write_content_vectors(STRING, UUID, VECTOR({dimensions}))",
    )
    functions = (
        *definers,
        "sync_direct_vector()",
        "sync_entity_vector()",
        "sync_fact_vector()",
        "refresh_content_vectors()",
    )
    # CockroachDB v26.2 parses and displays the CREATE clause before its privilege switch is
    # active. The explicit ALTER activates each narrow definer helper before triggers call it.
    for function in definers:
        op.execute(f"ALTER FUNCTION aizk_private.{function} SECURITY DEFINER")
    for table, kind in _DIRECT_SOURCES.items():
        op.execute(
            f"CREATE TRIGGER aizk_cspann_{table}_sync "
            f"AFTER INSERT OR UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION "
            f"aizk_private.sync_direct_vector('{kind}')"
        )
    op.execute(
        "CREATE TRIGGER aizk_cspann_entity_claim_sync "
        "AFTER INSERT OR UPDATE OR DELETE ON public.entity_claim "
        "FOR EACH ROW EXECUTE FUNCTION aizk_private.sync_entity_vector()"
    )
    op.execute(
        "CREATE TRIGGER aizk_cspann_fact_claim_sync "
        "AFTER INSERT OR UPDATE OR DELETE ON public.fact_claim "
        "FOR EACH ROW EXECUTE FUNCTION aizk_private.sync_fact_vector()"
    )
    op.execute(
        "CREATE TRIGGER aizk_cspann_entity_content_sync "
        "AFTER UPDATE ON public.entity_content "
        "FOR EACH ROW EXECUTE FUNCTION aizk_private.refresh_content_vectors('entity')"
    )
    op.execute(
        "CREATE TRIGGER aizk_cspann_fact_content_sync "
        "AFTER UPDATE ON public.fact_content "
        "FOR EACH ROW EXECUTE FUNCTION aizk_private.refresh_content_vectors('fact')"
    )
    for function in functions:
        op.execute(f"REVOKE ALL ON FUNCTION aizk_private.{function} FROM PUBLIC")
    for function in definers:
        op.execute(f"GRANT EXECUTE ON FUNCTION aizk_private.{function} TO {app_role}")
    op.execute("REVOKE ALL ON TABLE aizk_private.scoped_vector FROM PUBLIC")
    op.execute(f"REVOKE ALL ON TABLE aizk_private.scoped_vector FROM {app_role}")


def drop() -> None:
    """Remove the private C-SPANN capability and every synchronization trigger."""
    for table in _DIRECT_SOURCES:
        op.execute(f"DROP TRIGGER IF EXISTS aizk_cspann_{table}_sync ON public.{table}")
    for table in ("entity_claim", "fact_claim", "entity_content", "fact_content"):
        op.execute(f"DROP TRIGGER IF EXISTS aizk_cspann_{table}_sync ON public.{table}")
    op.execute("DROP FUNCTION IF EXISTS aizk_private.refresh_content_vectors()")
    op.execute("DROP FUNCTION IF EXISTS aizk_private.sync_fact_vector()")
    op.execute("DROP FUNCTION IF EXISTS aizk_private.sync_entity_vector()")
    op.execute("DROP FUNCTION IF EXISTS aizk_private.sync_direct_vector()")
    op.execute(
        f"DROP FUNCTION IF EXISTS aizk_private.write_content_vectors("
        f"STRING, UUID, VECTOR({settings.embed_dim}))"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS aizk_private.write_vector("
        f"STRING, UUID, UUID, UUID[], VECTOR({settings.embed_dim}), BOOL)"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS aizk_private.cspann_search("
        f"STRING, UUID[], VECTOR({settings.embed_dim}), INT8)"
    )
    op.execute("DROP FUNCTION IF EXISTS aizk_private.cspann_scopes(STRING)")
    op.execute("DROP TABLE IF EXISTS aizk_private.scoped_vector")
    op.execute("DROP SCHEMA IF EXISTS aizk_private")
