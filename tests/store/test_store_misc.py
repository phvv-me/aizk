import ssl
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import dbutil
import pytest
from id_factory import uuid5, uuid8
from patos.sql import CosineHalfvec
from pydantic import UUID5, ValidationError
from rls import Catalog, Command, CompiledPolicy, RLSState
from rls.ddl import RLSAction, RLSStatement
from sqlalchemy import ColumnElement, Integer, MetaData, Table, Uuid, any_, column, delete
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import SessionTransaction
from sqlalchemy_cockroachdb.asyncpg import CockroachDBDialect_asyncpg
from sqlmodel import select

import aizk.store.backend as backend
import aizk.store.security as security
from aizk.config import DatabaseBackend, settings
from aizk.exceptions import NoTenantContext
from aizk.retrieval.models.lane import QueryContext
from aizk.store import Chunk, Document, Entity, Fact, TableBase, id_array
from aizk.store.backend import (
    CockroachDBAdapter,
    PostgreSQLAdapter,
    bind_cockroach_authority,
    database_adapter,
)
from aizk.store.ddl import CreateView, DropView, Grant, GrantTarget, postgresql_sql
from aizk.store.engine import Database, DatabaseRole, Session
from aizk.store.identity import User
from aizk.store.migrations.cockroachdb import scoped_cspann
from aizk.store.mixins.scoped import Scoped, Standing
from aizk.store.mixins.view import ViewBase
from aizk.store.security import RLSVerifier
from aizk.store.vector import CosineVector, cosine_distance, scoped_vector_candidates


class RecordingTLSContext:
    def __init__(self) -> None:
        self.check_hostname = True
        self.certificate: str | bytes | None = None

    def load_verify_locations(
        self,
        cafile: str | None = None,
        capath: str | None = None,
        cadata: str | bytes | None = None,
    ) -> None:
        del cafile, capath
        self.certificate = cadata


def test_created_at_expression_compiles_to_recorded_from() -> None:
    compiled = str(select(Fact.Claim.created_at)).lower()
    assert "fact_claim.recorded_from" in compiled


def test_view_ddl_round_trips_create_and_drop() -> None:
    view = CreateView(
        Fact.Live.__view_select__(),
        "live_fact",
        postgresql_with={"security_invoker": True},
    )
    ddl = postgresql_sql(view)
    assert ddl.startswith("CREATE VIEW live_fact WITH (security_invoker = true) AS")
    assert postgresql_sql(DropView(view.table, if_exists=True)) == "DROP VIEW IF EXISTS live_fact"


def test_view_ddl_without_options_uses_native_sqlalchemy_rendering() -> None:
    view = CreateView(Fact.Live.__view_select__(), "plain_live_fact")

    assert postgresql_sql(view).startswith("CREATE VIEW plain_live_fact AS")


def test_cockroach_ddl_quotes_grants_views_and_row_security() -> None:
    dialect = CockroachDBDialect_asyncpg()
    table = Table("private items", MetaData())
    policy = CompiledPolicy(
        name="scope read",
        command=Command.select,
        using="true",
        check="true",
        roles=("app role",),
        permissive=False,
    )
    statements = (
        Grant(GrantTarget.table, "private items", "app role", ("SELECT", "UPDATE")),
        RLSStatement(table, RLSAction.create, policy=policy),
        RLSStatement(table, RLSAction.enable),
        RLSStatement(table, RLSAction.drop, name="scope read"),
    )
    rendered = [str(statement.compile(dialect=dialect)) for statement in statements]

    assert rendered[0] == 'GRANT SELECT, UPDATE ON "private items" TO "app role"'
    assert "AS RESTRICTIVE" in rendered[1]
    assert "USING (true)" in rendered[1] and "WITH CHECK (true)" in rendered[1]
    assert rendered[2] == 'ALTER TABLE "private items" ENABLE ROW LEVEL SECURITY'
    assert rendered[3] == 'DROP POLICY IF EXISTS "scope read" ON "private items"'
    view = CreateView(select(Document.id), "portable", postgresql_with={"check_option": None})
    assert "WITH (check_option)" in str(view.compile(dialect=dialect))


def test_cockroach_operator_policy_uses_the_session_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)

    rendered = str(
        Standing.operator().compile(
            dialect=CockroachDBDialect_asyncpg(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "split_part" in rendered
    assert "application_name" in rendered
    assert " AS BOOLEAN" in rendered
    assert ", 5)" in rendered
    assert "SELECT" not in rendered


def test_cockroach_rls_verifier_accepts_deparser_casts_and_reports_open_tables() -> None:
    managed = Table("managed_items", MetaData())
    unmanaged = Table("open_items", managed.metadata)
    empty = Table("empty_items", managed.metadata)
    declared_policy = CompiledPolicy(
        name="scope_read",
        command=Command.select,
        using="cardinality(scopes) > CAST(0 AS INT8) "
        "AND current_setting('app:::uid'::STRING, true) <> ''",
    )
    live_policy = declared_policy.model_copy(
        update={
            "using": "((cardinality(scopes) > 0:::INT8) AND "
            "(current_setting('app:::uid':::STRING, true) <> '':::STRING))"
        }
    )
    declared = {managed: RLSState(policies=(declared_policy,))}
    live = {
        managed: RLSState(policies=(live_policy,)),
        unmanaged: RLSState(
            policies=(CompiledPolicy(name="unexpected", command=Command.select, using="true"),)
        ),
        empty: RLSState(enabled=False, forced=False),
    }
    catalog = cast(
        Catalog,
        SimpleNamespace(
            tables=(managed, unmanaged, empty),
            state=declared.get,
            inspect=lambda connection: live,
            verify=lambda connection: ["postgres delegated"],
        ),
    )
    verifier = RLSVerifier(catalog)
    postgres = cast(Connection, SimpleNamespace(dialect=postgresql_dialect()))
    cockroach = cast(
        Connection,
        SimpleNamespace(
            dialect=CockroachDBDialect_asyncpg(),
            execute=lambda statement: [
                ("public", "managed_items", "scope_read", "permissive"),
                ("public", "open_items", "unexpected", "permissive"),
            ],
        ),
    )

    assert verifier.verify(postgres) == ["postgres delegated"]
    assert verifier.verify(cockroach) == ["open_items has undeclared row level security"]


def test_cockroach_policy_translation_preserves_quoted_triple_colons() -> None:
    clause = "'a'':::b':::STRING || \"a\"\":::b\":::STRING || $$c:::d$$:::STRING"

    assert security._CockroachPolicy.postgres_casts(clause) == (
        "'a'':::b'::STRING || \"a\"\":::b\"::STRING || $$c:::d$$::STRING"
    )
    declared = security._CockroachPolicy(
        name="visible", command=Command.select, using="aizk_visible(id)"
    )
    reflected = declared.model_copy(update={"using": "public.aizk_visible(id)"})
    assert declared.matches(reflected, "items")


def test_cockroach_migrations_are_one_native_revision() -> None:
    versions = (
        Path(__file__).parents[2]
        / "src"
        / "aizk"
        / "store"
        / "migrations"
        / "cockroachdb"
        / "versions"
    )

    assert [path.name for path in versions.glob("[0-9]*.py")] == ["0001_cockroachdb.py"]


def test_scoped_cspann_function_keeps_authority_outside_the_ann_query() -> None:
    rendered = scoped_cspann.search_function(settings.embed_dim)
    ann = rendered[rendered.index("RETURN QUERY") :]

    assert "current_setting" not in ann
    assert "candidate.kind = requested_kind" in ann
    assert "candidate.scopes = requested_scopes" in ann
    assert "embedding <=> query_vector" in ann


def test_scoped_cspann_creation_reasserts_security_definer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    monkeypatch.setattr(
        scoped_cspann.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=CockroachDBDialect_asyncpg()),
    )
    monkeypatch.setattr(scoped_cspann.op, "execute", statements.append)

    scoped_cspann.create()

    altered = [statement for statement in statements if statement.startswith("ALTER FUNCTION")]
    assert altered == [
        "ALTER FUNCTION aizk_private.cspann_scopes(STRING) SECURITY DEFINER",
        "ALTER FUNCTION aizk_private.cspann_search(STRING, UUID[], VECTOR(1024), INT8) "
        "SECURITY DEFINER",
        "ALTER FUNCTION aizk_private.write_vector(STRING, UUID, UUID, UUID[], "
        "VECTOR(1024), BOOL) SECURITY DEFINER",
        "ALTER FUNCTION aizk_private.write_content_vectors(STRING, UUID, VECTOR(1024)) "
        "SECURITY DEFINER",
    ]
    trigger_positions = [
        index
        for index, statement in enumerate(statements)
        if statement.startswith("CREATE TRIGGER")
    ]
    assert max(statements.index(statement) for statement in altered) < min(trigger_positions)


def test_database_adapters_build_pools_and_bind_cockroach_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgresql = PostgreSQLAdapter()
    cockroach = CockroachDBAdapter()
    monkeypatch.setattr(settings, "db_null_pool", True)
    null_engine = postgresql.engine(settings.database_url, app_role=True)
    assert null_engine.pool.__class__.__name__ == "NullPool"
    null_engine.sync_engine.dispose()

    monkeypatch.setattr(settings, "vchordrq_prefilter", True)
    monkeypatch.setattr(settings, "bm25_limit", 210)
    assert postgresql.server_settings() == {
        "vchordrq.prefilter": "on",
        "bm25_catalog.bm25_limit": "210",
    }
    monkeypatch.setattr(settings, "vchordrq_prefilter", False)
    assert postgresql.server_settings()["vchordrq.prefilter"] == "off"

    monkeypatch.setattr(settings, "db_null_pool", False)
    owner_engine = postgresql.engine(settings.admin_database_url, app_role=False)
    pooled_engine = cockroach.engine(settings.database_url, app_role=True)
    assert owner_engine.pool.__class__.__name__ != "NullPool"
    assert pooled_engine.pool.__class__.__name__ != "NullPool"
    owner_engine.sync_engine.dispose()
    pooled_engine.sync_engine.dispose()

    read_scope = uuid5()
    user = User.authorized(uuid5(), read=(read_scope,), write=(uuid5(),), public=(uuid5(),))
    session = OrmSession()
    cockroach.configure_session(session, user)
    connection = cast(Connection, RecordingConnection())
    bind_cockroach_authority(session, cast("SessionTransaction", None), connection)
    recorder = cast(RecordingConnection, connection)
    assert recorder.parameters is not None
    assert str(read_scope) in recorder.parameters["aizk_cockroach_authority"]
    assert recorder.parameters["aizk_cockroach_authority"].endswith("|false")

    untouched = RecordingConnection()
    bind_cockroach_authority(
        OrmSession(),
        cast("SessionTransaction", None),
        cast(Connection, untouched),
    )
    assert untouched.parameters is None

    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.postgresql)
    assert isinstance(database_adapter(), PostgreSQLAdapter)
    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)
    assert isinstance(database_adapter(), CockroachDBAdapter)
    monkeypatch.setattr(settings, "database_backend", cast(DatabaseBackend, "unsupported"))
    with pytest.raises(ValueError, match="unsupported database backend"):
        database_adapter()


def test_cockroach_cloud_urls_use_verified_asyncpg_tls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contexts: list[RecordingTLSContext] = []

    def create_context() -> ssl.SSLContext:
        context = RecordingTLSContext()
        contexts.append(context)
        return cast("ssl.SSLContext", context)

    monkeypatch.setattr(backend.ssl, "create_default_context", create_context)
    monkeypatch.setattr(settings, "db_ssl_root_certificate", "operator certificate")
    normalized, configured = CockroachDBAdapter.cloud_connection(
        "cockroachdb+asyncpg://user@cluster/aizk?application_name=ccloud"
        "&sslmode=verify-full&sslrootcert=/missing"
    )
    assert normalized.query == {}
    assert configured is contexts[-1]
    assert contexts[-1].check_hostname is True
    assert contexts[-1].certificate == "operator certificate"

    certificate = tmp_path / "root.crt"
    certificate.write_text("ccloud certificate")
    monkeypatch.setattr(settings, "db_ssl_root_certificate", "")
    normalized, configured = CockroachDBAdapter.cloud_connection(
        f"cockroachdb+asyncpg://user@cluster/aizk?sslmode=verify-ca&sslrootcert={certificate}"
    )
    assert normalized.query == {}
    assert configured is contexts[-1]
    assert contexts[-1].check_hostname is False
    assert contexts[-1].certificate == "ccloud certificate"

    _, configured = CockroachDBAdapter.cloud_connection(
        "cockroachdb+asyncpg://user@cluster/aizk?sslmode=verify-full"
    )
    assert configured is contexts[-1]
    assert contexts[-1].certificate is None

    _, configured = CockroachDBAdapter.cloud_connection(
        "cockroachdb+asyncpg://user@cluster/aizk?sslmode=verify-full&sslrootcert=/missing"
    )
    assert configured is contexts[-1]
    assert contexts[-1].certificate is None


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("disable", False), ("require", "require")],
)
def test_cockroach_cloud_urls_translate_nonverifying_ssl_modes(
    mode: str,
    expected: bool | str,
) -> None:
    normalized, configured = CockroachDBAdapter.cloud_connection(
        f"cockroachdb+asyncpg://user@cluster/aizk?sslmode={mode}"
    )
    assert normalized.query == {}
    assert configured == expected


def test_cockroach_cloud_urls_reject_unknown_ssl_modes() -> None:
    with pytest.raises(ValueError, match="unsupported CockroachDB sslmode prefer"):
        CockroachDBAdapter.cloud_connection(
            "cockroachdb+asyncpg://user@cluster/aizk?sslmode=prefer"
        )


class RecordingConnection:
    """Record one SQLAlchemy execution without opening a database connection."""

    def __init__(self) -> None:
        self.parameters: dict[str, str] | None = None

    def execute(self, statement, parameters: dict[str, str]) -> None:
        del statement
        self.parameters = parameters


def test_portable_vectors_and_chunk_retrieval_compile_for_cockroach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)
    context = QueryContext(dimensions=settings.embed_dim, fuzzy=False)
    embedded = cast("ColumnElement[Sequence[float]]", Chunk.__table__.c.embedding)
    compared = CosineVector.Comparator(embedded).__matmul__([0.0] * settings.embed_dim)
    statement = select(
        cosine_distance(Chunk.embedding, context.vector),
        compared,
    ).select_from(Chunk)
    dialect = CockroachDBDialect_asyncpg()
    rendered = str(statement.compile(dialect=dialect))
    fused = str(Chunk.fused(context).compile(dialect=dialect))

    assert rendered.count("<=>") == 2
    assert "plainto_tsquery" in fused
    assert "to_bm25query" not in fused
    assert isinstance(Chunk.__table__.c.embedding.type, CosineHalfvec)  # import-time postgres
    assert isinstance(context.vector.type, CosineVector)  # runtime cockroach bind


def test_cockroach_owned_vector_search_uses_one_exact_scope_partition() -> None:
    vector = cast(
        "ColumnElement[list[float]]",
        column("query_vector", CosineVector(settings.embed_dim)),
    )
    limit = cast("ColumnElement[int]", column("query_limit", Integer()))
    scopes = cast(
        "ColumnElement[list[UUID5]]",
        column("query_scopes", ARRAY(Uuid())),
    )

    rendered = str(
        scoped_vector_candidates("chunk", vector, limit, scopes).compile(
            dialect=CockroachDBDialect_asyncpg()
        )
    )

    assert "aizk_private.cspann_search" in rendered
    assert "aizk_private.cspann_scopes" not in rendered
    assert "query_scopes" in rendered


def test_abstract_store_types_remain_unmapped_and_unregistered() -> None:
    with pytest.raises(NotImplementedError):
        ViewBase.__view_select__()

    class AbstractView(ViewBase):
        pass

    assert getattr(AbstractView, "__mapper__", None) is None
    before = {
        table for table in TableBase.metadata.tables.values() if Catalog.state(table) is not None
    }

    class AbstractScoped(Scoped):
        pass

    after = {
        table for table in TableBase.metadata.tables.values() if Catalog.state(table) is not None
    }
    assert after == before


def test_ownership_counting_reads_each_backend_carrier_of_the_caller_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counting drops a row held entirely in unwritable public scopes, on either backend."""
    native = str(Standing.counted(Document.scopes).compile(dialect=postgresql_dialect()))
    assert "jsonb_array_elements_text" in native
    assert "NOT (document.scopes <@ array(" in native
    assert "!= ALL (array(" in native

    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)
    portable = str(Standing.counted(Document.scopes).compile(dialect=CockroachDBDialect_asyncpg()))
    assert portable.count("split_part(current_setting") == 2
    assert "NOT (document.scopes <@ array(" in portable


@pytest.mark.parametrize("role", list(DatabaseRole))
def test_database_uses_a_real_pool_when_not_null_pooled(
    monkeypatch: pytest.MonkeyPatch, role: DatabaseRole
) -> None:
    monkeypatch.setattr(settings, "db_null_pool", False)
    engine = Database(role).engine
    try:
        assert engine.pool.__class__.__name__ != "NullPool"
    finally:
        engine.sync_engine.dispose()


def test_document_record_excludes_derived_columns() -> None:
    doc = Document(content_hash=uuid8(), created_by=uuid5())
    record = doc.record()
    assert doc.id.version == 7
    assert record["table"] == "document"
    assert "embedding" not in record and "tsv" not in record


def test_document_content_identity_requires_uuid8() -> None:
    owner = uuid5()
    digest = uuid8()
    document = Document.model_validate(
        {"content_hash": digest, "created_by": owner, "scopes": [owner]}
    )
    assert document.content_hash == digest

    with pytest.raises(ValidationError, match="UUID version 8 expected"):
        Document.model_validate({"content_hash": uuid5(), "created_by": owner, "scopes": [owner]})


def test_session_requires_a_bound_user() -> None:
    with pytest.raises(NoTenantContext, match="database session has no user"):
        _ = Session().user


def test_scope_boundaries_store_one_nonempty_canonical_key(migrated_db: None) -> None:
    first, second = sorted((uuid5(), uuid5()))
    assert User.authorized(first, read=(second, first, second)).scopes.read == frozenset(
        {first, second}
    )

    async def persist() -> None:
        async with User.system().owner as session:
            document = Document(
                created_by=first,
                scopes=[second, first, second],
                content_hash=uuid8(),
            )
            session.add(document)
            await session.flush()
            stored = await session.scalar(
                select(Document.scopes).where(Document.id == document.id)
            )
            assert document.scopes == stored == [first, second]
            await session.rollback()

        async with User.system().owner as session:
            session.add(Document(created_by=first, scopes=[], content_hash=uuid8()))
            with pytest.raises(ValueError, match="scopes cannot be empty"):
                await session.flush()
            await session.rollback()

    dbutil.run(persist())


def test_direct_source_identity_requires_a_complete_normalized_title(
    migrated_db: None,
) -> None:
    owner = uuid5()

    async def match(query: str, titles: list[str]) -> dict[str, bool]:
        async with dbutil.actor(owner) as session:
            documents = [
                Document(
                    title=title,
                    created_by=owner,
                    scopes=[owner],
                    content_hash=uuid8(),
                )
                for title in titles
            ]
            session.add_all(documents)
            await session.flush()
            rows = await session.exec(
                select(Document.title, Document.named_in_query()).where(
                    Document.id.in_([document.id for document in documents])
                ),
                params={"qtext": query},
            )
            await session.rollback()
        return {title: direct for title, direct in rows}

    assert dbutil.run(
        match(
            "What are the current Research projects in Open SWE Book.md?",
            ["en", "Research", "Open SWE Book.md", "SWE Book"],
        )
    ) == {
        "en": False,
        "Research": True,
        "Open SWE Book.md": True,
        "SWE Book": True,
    }


def test_an_id_set_travels_as_one_parameter_however_many_ids_it_holds() -> None:
    """A bind per id has a 32767 ceiling a large scope's entity roster walks straight past."""
    ids = [uuid5() for _ in range(5_000)]

    names = Entity.Content.names_of(ids).compile(dialect=postgresql_dialect())
    deletion = (
        delete(Fact.Content)
        .where(Fact.Content.id == any_(id_array(ids)))
        .compile(dialect=postgresql_dialect())
    )

    assert len(names.params) == len(deletion.params) == 1
    assert "= ANY (CAST(" in str(names)
    assert "= ANY (CAST(" in str(deletion)
