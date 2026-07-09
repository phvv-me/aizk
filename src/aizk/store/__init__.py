# importing the `rls` library arms its global mapper-construction hook and registers its alembic
# operations, comparator, and renderers as a side effect, so `op.apply_scoped_rls` exists and the
# autogenerate guard runs before any migration or autogenerate pass.
import rls

from ..exceptions import NoTenantContext

# importing events registers the after_begin and do_orm_execute listeners as a side effect, the
# same import-for-effect contract `rls` carries for its alembic operations.
from . import events as events
from .engine import acting_as, app_sessions, as_system
from .mixins import TableBase
from .models import (
    Chunk,
    Community,
    Document,
    EntityClaim,
    EntityContent,
    EntityKind,
    FactClaim,
    FactContent,
    LiveFact,
    Profile,
    RelationKind,
    SessionItem,
    Watermark,
)

# populate the shared row-level-security registry once every model above is mapped. `rls.register`
# backfills `TableBase.metadata.info["rls_policies"]`, the `["rls"]` protected-table set, and
# `["rls_grant_role"]` from each model's own `__rls_policies__`, reading them with all sibling
# model names already bound (so a content table's read-through-claim policy resolves its claim
# class), and remembers the metadata so a table-name-only `op.apply_scoped_rls` can recover a
# table's policies at migration time. It runs before any query, autogenerate pass, or migration
# reads the registry.
rls.register(TableBase, grant_role="aizk_app")

# re-exported from `rls` so callers keep reading it off `aizk.store`; the no-leak verify itself is
# generic and lives in the library.
verify_scoped_rls = rls.verify_scoped_rls

__all__ = [
    "Chunk",
    "Community",
    "Document",
    "EntityClaim",
    "EntityContent",
    "EntityKind",
    "FactClaim",
    "FactContent",
    "LiveFact",
    "NoTenantContext",
    "Profile",
    "RelationKind",
    "SessionItem",
    "TableBase",
    "Watermark",
    "acting_as",
    "app_sessions",
    "as_system",
    "verify_scoped_rls",
]
