from ..exceptions import NoTenantContext

# isort: off
# `rls` has to import before any model module ever constructs a mapped class: importing it
# registers `store.rls.register`'s `after_mapper_constructed` hook, which reads a class's own
# `__rls_policies__` the instant its class statement finishes, so a model mapped before this line
# runs would carry no entry in `TableBase.metadata.info["rls_policies"]` at all. `events` below is
# the first import that would otherwise reach `.models` (through its own `FactClaim` import), so
# this line has to stay ahead of it rather than sort alphabetically after it.
from . import rls as rls

# importing events registers the after_begin and do_orm_execute listeners as a side effect, the
# same import-for-effect contract `rls` carries for its alembic operations.
from . import events as events

# isort: on
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
    Group,
    LiveFact,
    Membership,
    Profile,
    RelationKind,
    SessionItem,
    User,
    Watermark,
)
from .rls import verify_scoped_rls

__all__ = [
    "Chunk",
    "Community",
    "Document",
    "EntityClaim",
    "EntityContent",
    "EntityKind",
    "FactClaim",
    "FactContent",
    "Group",
    "LiveFact",
    "Membership",
    "NoTenantContext",
    "User",
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
