import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

from sqlalchemy import Index

import aizk
from aizk.config import Settings
from aizk.store import EntityContent

# the migration is loaded by path since it lives outside the importable package, the second DDL
# source whose index-backend branch must track the ORM `embedding_index` as the width does
MIGRATION_PATH = Path(aizk.__file__).parent / "store" / "migrations" / "versions" / "0001_init.py"


def migration_module() -> ModuleType:
    """Load the initial migration by path so its module-level backend and DDL helpers can be read.

    Only the module body runs, never `upgrade`, so no alembic operation touches the database.
    """
    spec = importlib.util.spec_from_file_location("aizk_migration_0001", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_backends_are_the_vchord_pair() -> None:
    """The default keeps the RAM-frugal vchordrq index and the vchord_bm25 lexical lane."""
    settings = Settings()
    assert settings.index_backend == "vchordrq"
    assert settings.bm25_backend == "vchord_bm25"


def orm_embedding_index() -> Index:
    """The ann index a mapped Embedded table actually carries, the ORM-side DDL source."""
    return next(
        index
        for index in EntityContent.__table__.indexes
        if index.name == "ix_entity_content_embedding"
    )


def test_orm_index_uses_the_configured_backend() -> None:
    """The mapped embedding index names the configured access method and the cosine opclass."""
    index = orm_embedding_index()
    assert index.kwargs["postgresql_using"] == Settings().index_backend
    assert index.kwargs["postgresql_ops"] == {"embedding": "halfvec_cosine_ops"}


def test_both_ddl_sources_agree_on_the_index_backend() -> None:
    """The ORM DDL source and the migration module read one settings value, so they never drift."""
    module = migration_module()
    index = orm_embedding_index()
    assert index.kwargs["postgresql_using"] == module.INDEX_BACKEND


def test_vchord_defaults_emit_the_vchordrq_and_bm25_ddl() -> None:
    """Under the vchord defaults the migration emits the vchordrq index and the bm25 lane DDL."""
    module = migration_module()

    assert "USING vchordrq (embedding halfvec_cosine_ops)" in module.vector_index_ddl(
        "ix_chunk_embedding", "chunk", module.INDEX_BACKEND
    )
    assert module.required_extensions("vchordrq", "vchord_bm25") == (
        "vector",
        "pg_trgm",
        "pgcrypto",
        "vchord",
        "vchord_bm25",
        "pg_tokenizer",
    )
    statements = module.bm25_lexical_statements()
    joined = "\n".join(statements)
    assert "create_tokenizer('aizk_bm25'" in joined
    assert "ADD COLUMN bm25 bm25vector" in joined
    assert "CREATE INDEX ix_chunk_bm25 ON chunk USING bm25 (bm25 bm25_ops)" in joined
    assert "CREATE TRIGGER chunk_bm25_sync" in joined
    assert any("GRANT USAGE ON SCHEMA" in statement for statement in statements)


def test_portable_fallback_emits_the_hnsw_and_tsvector_ddl() -> None:
    """The hnsw + tsvector fallback emits the native index DDL and pulls no vchord extension."""
    module = migration_module()

    assert "USING hnsw (embedding halfvec_cosine_ops)" in module.vector_index_ddl(
        "ix_chunk_embedding", "chunk", "hnsw"
    )
    assert module.required_extensions("hnsw", "tsvector") == ("vector", "pg_trgm", "pgcrypto")


def test_the_hnsw_tsvector_backend_flows_to_both_ddl_sources() -> None:
    """Pinning the portable backends rebuilds the ORM index and the migration in one process.

    Run in a fresh interpreter since both DDL sources read the backend once at import, so the
    pinned environment has to be set before either module loads, which a subprocess guarantees.
    """
    probe = textwrap.dedent(
        f"""
        import importlib.util
        from aizk.config import Settings
        from aizk.store import EntityContent

        assert Settings().index_backend == "hnsw", Settings().index_backend
        assert Settings().bm25_backend == "tsvector", Settings().bm25_backend
        index = next(
            i for i in EntityContent.__table__.indexes if i.name == "ix_entity_content_embedding"
        )
        assert index.kwargs["postgresql_using"] == "hnsw"

        spec = importlib.util.spec_from_file_location("m", {str(MIGRATION_PATH)!r})
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.INDEX_BACKEND == "hnsw", module.INDEX_BACKEND
        assert module.BM25_BACKEND == "tsvector", module.BM25_BACKEND
        ddl = module.vector_index_ddl("ix_chunk_embedding", "chunk", module.INDEX_BACKEND)
        assert "USING hnsw" in ddl, ddl
        assert module.required_extensions(module.INDEX_BACKEND, module.BM25_BACKEND) == (
            "vector",
            "pg_trgm",
            "pgcrypto",
        )
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "AIZK_INDEX_BACKEND": "hnsw", "AIZK_BM25_BACKEND": "tsvector"},
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
