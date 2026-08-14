import rls

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

verify_rls = rls.Catalog(TableBase.mapper_registry).verify


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
