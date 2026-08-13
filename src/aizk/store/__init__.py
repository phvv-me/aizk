import rls
from sqlalchemy.engine import Connection

from ..exceptions import NoTenantContext
from . import events as events
from .binds import id_array
from .mixins import TableBase
from .models import (
    Artifact,
    Blob,
    Chunk,
    Community,
    Document,
    Entity,
    Explorer,
    Fact,
    Knowledge,
    OperatorReading,
    OperatorSnapshot,
    Profile,
    Relation,
    SessionItem,
    UploadCapability,
    Usage,
    UsageEvent,
    Watermark,
)
from .security import RLSVerifier

_catalog = rls.Catalog(TableBase.mapper_registry)
_rls_verifier = RLSVerifier(_catalog)


def verify_rls(connection: Connection) -> list[str]:
    """Report drift from Aizk's complete row security declaration."""
    return _rls_verifier.verify(connection)


__all__ = [
    "Artifact",
    "Blob",
    "Chunk",
    "Community",
    "Document",
    "Entity",
    "Explorer",
    "Fact",
    "Knowledge",
    "NoTenantContext",
    "Profile",
    "Relation",
    "SessionItem",
    "UploadCapability",
    "TableBase",
    "Usage",
    "UsageEvent",
    "OperatorReading",
    "OperatorSnapshot",
    "Watermark",
    "id_array",
    "verify_rls",
]
