import rls
from sqlalchemy.engine import Connection

from ..exceptions import NoTenantContext
from . import events as events
from .mixins import TableBase
from .models import (
    Chunk,
    Community,
    Document,
    Entity,
    Fact,
    Profile,
    Relation,
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
    "Entity",
    "Fact",
    "NoTenantContext",
    "Profile",
    "Relation",
    "SessionItem",
    "TableBase",
    "Watermark",
    "verify_rls",
]
