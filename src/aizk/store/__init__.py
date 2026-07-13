import rls
from sqlalchemy.engine import Connection

from ..exceptions import NoTenantContext
from . import events as events
from .engine import (
    as_system,
    session_for,
)
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

_catalog = rls.Catalog(TableBase.mapper_registry)


def verify_rls(connection: Connection) -> list[str]:
    """Report drift from Aizk's complete row security declaration."""
    return _catalog.verify(connection)


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
    "as_system",
    "session_for",
    "verify_rls",
]
