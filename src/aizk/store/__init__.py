import rls
from sqlalchemy.engine import Connection

from ..exceptions import NoTenantContext
from . import events as events
from .mixins import TableBase
from .models import (
    Artifact,
    Blob,
    Chunk,
    Community,
    Document,
    Entity,
    Fact,
    Knowledge,
    Profile,
    Relation,
    SessionItem,
    UploadCapability,
    Usage,
    Watermark,
)

_catalog = rls.Catalog(TableBase.mapper_registry)


def verify_rls(connection: Connection) -> list[str]:
    """Report drift from Aizk's complete row security declaration."""
    return _catalog.verify(connection)


__all__ = [
    "Artifact",
    "Blob",
    "Chunk",
    "Community",
    "Document",
    "Entity",
    "Fact",
    "Knowledge",
    "NoTenantContext",
    "Profile",
    "Relation",
    "SessionItem",
    "UploadCapability",
    "Usage",
    "TableBase",
    "Watermark",
    "verify_rls",
]
