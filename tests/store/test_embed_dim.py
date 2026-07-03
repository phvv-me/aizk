import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pgvector.sqlalchemy import HALFVEC

import aizk
from aizk.config import Settings
from aizk.store.models import Chunk, Community, EntityContent, FactContent, Profile

# the migration file lives outside the importable package, so it is loaded by path to read the
# width it builds its embedding columns at, the second DDL source that must track the ORM mixin
MIGRATION_PATH = Path(aizk.__file__).parent / "store" / "migrations" / "versions" / "0001_init.py"

# every mapped model that mixes in the Embedded halfvec column, the ORM DDL source autogenerate
# compares the migration against; the split moved the embedding column onto the content half of
# entity and fact, so those are the classes carrying it now, not their claim counterparts
EMBEDDED_MODELS = (Chunk, EntityContent, FactContent, Community, Profile)


def migration_embed_dim() -> int:
    """Load the initial migration by path and read the halfvec width it creates columns at.

    The migration is not part of the importable package, so it is loaded from its file, and only
    its module-level `EMBED_DIM` is read, never `upgrade`, so no alembic operation runs.
    """
    spec = importlib.util.spec_from_file_location("aizk_migration_0001", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EMBED_DIM


def test_default_embed_dim_is_1024() -> None:
    """The default keeps the multilingual-e5-large text width, so the schema is 1024 by default."""
    assert Settings().embed_dim == 1024


def test_every_embedded_column_follows_settings_embed_dim() -> None:
    """Each mapped halfvec column is built at the configured width, the ORM DDL source of truth."""
    width = Settings().embed_dim
    for model in EMBEDDED_MODELS:
        column_type = model.__table__.c.embedding.type
        assert isinstance(column_type, HALFVEC)  # narrow to the pgvector type that carries `dim`
        assert column_type.dim == width


def test_the_migration_builds_columns_at_settings_embed_dim() -> None:
    """The migration reads the same width the ORM mixin does, so the DDL sources never drift."""
    assert migration_embed_dim() == Settings().embed_dim


def test_a_512_width_flows_to_both_ddl_sources() -> None:
    """Pinning AIZK_EMBED_DIM=512 rebuilds the ORM column and the migration at 512 in one process.

    Run in a fresh interpreter since both DDL sources read the width once at import, so the pinned
    environment has to be set before either module loads, which a subprocess guarantees.
    """
    probe = textwrap.dedent(
        f"""
        import importlib.util
        from aizk.config import Settings
        from aizk.store.models import Chunk

        assert Settings().embed_dim == 512, Settings().embed_dim
        assert Chunk.__table__.c.embedding.type.dim == 512, Chunk.__table__.c.embedding.type.dim

        spec = importlib.util.spec_from_file_location("m", {str(MIGRATION_PATH)!r})
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.EMBED_DIM == 512, module.EMBED_DIM
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "AIZK_EMBED_DIM": "512"},
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


@pytest.mark.parametrize("model", EMBEDDED_MODELS)
def test_embedded_column_is_nullable_halfvec(model: type) -> None:
    """Every embedded column stays a nullable halfvec, null until the row is embedded."""
    column = model.__table__.c.embedding
    assert column.nullable
    assert column.type.dim == Settings().embed_dim
