from dataclasses import dataclass
from datetime import datetime
from typing import Self

from pydantic import UUID8

from ..artifacts.configured import build_artifact_services, build_byte_store
from ..artifacts.models import ArtifactReceipt
from ..artifacts.service import ArtifactIntake
from ..artifacts.uploads import UploadBox
from ..auth import Auth
from ..config import Settings
from ..integrations.docling import ArtifactBytes
from ..integrations.logto import LogtoClient
from ..storage import ByteStore
from ..store import Blob
from ..store.identity import User
from ..types import ScopeNames, Scopes


class TextOnlyArtifacts:
    """Reject artifact operations in a deployment configured for text memories only."""

    async def uri(
        self,
        user: User,
        uri: str,
        scopes: ScopeNames | None = None,
        companion_text: str | None = None,
        observed_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ArtifactReceipt:
        del user, uri, scopes, companion_text, observed_at, expires_at
        raise RuntimeError("this deployment accepts text memories only")

    async def accept(
        self,
        user: User,
        artifact: ArtifactBytes,
        *,
        target: Scopes,
        companion_text: str | None = None,
    ) -> ArtifactReceipt:
        del user, artifact, target, companion_text
        raise RuntimeError("this deployment accepts text memories only")

    async def get(
        self,
        key: str,
        *,
        encoding: Blob.Encoding = Blob.Encoding.identity,
        expected_size: int | None = None,
        expected_hash: UUID8 | None = None,
        version: str | None = None,
    ) -> bytes:
        del key, encoding, expected_size, expected_hash, version
        raise RuntimeError("this deployment has no artifact byte store")


@dataclass(frozen=True)
class McpRuntime:
    """Build the bounded services used by the public Lambda process."""

    settings: Settings
    artifacts: TextOnlyArtifacts | ArtifactIntake
    uploads: UploadBox
    auth: Auth
    store: ByteStore | None = None

    @classmethod
    def assemble(cls, config: Settings) -> Self:
        """Build text-only or S3-backed artifact intake from explicit deployment settings."""
        if config.artifact_ingest_enabled:
            store = build_byte_store(config)
            services = build_artifact_services(config, store)
            artifacts: TextOnlyArtifacts | ArtifactIntake = services.intake
        else:
            store = None
            artifacts = TextOnlyArtifacts()
        return cls(
            settings=config,
            artifacts=artifacts,
            uploads=UploadBox.from_settings(config, artifacts),
            auth=Auth(LogtoClient(config), config),
            store=store,
        )

    @property
    def artifact_store(self) -> TextOnlyArtifacts | ByteStore:
        """Return the byte store matching the selected artifact mode."""
        if self.store is not None:
            return self.store
        if isinstance(self.artifacts, TextOnlyArtifacts):
            return self.artifacts
        raise RuntimeError("artifact intake requires a byte store")
