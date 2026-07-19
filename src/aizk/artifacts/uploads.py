import hashlib
import secrets
from collections.abc import AsyncIterable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol, Self, runtime_checkable

from patos import FlexModel, FrozenModel
from pydantic import (
    UUID5,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)
from pydantic.types import JsonValue
from sqlalchemy import delete, func
from sqlmodel import select

from ..config import Settings, settings
from ..integrations.docling import ArtifactBytes
from ..storage import ByteLimitExceeded
from ..store import UploadCapability
from ..store.identity import OrganizationStanding, ScopeTable, User
from ..types import ScopeNames
from .models import ArtifactReceipt

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


@runtime_checkable
class Intake(Protocol):
    """The one intake call an upload box delivers a claimed upload through."""

    async def accept(
        self,
        user: User,
        artifact: ArtifactBytes,
        *,
        scopes: ScopeNames | None = None,
        companion_text: str | None = None,
    ) -> ArtifactReceipt: ...


class InertIntake:
    """A real no-op intake for schema-only construction, never wired to live serving."""

    async def accept(
        self,
        user: User,
        artifact: ArtifactBytes,
        *,
        scopes: ScopeNames | None = None,
        companion_text: str | None = None,
    ) -> ArtifactReceipt:
        raise RuntimeError("this upload box was built for schema generation only")


class UploadCapabilityError(LookupError):
    """The upload capability is unknown, already used, or expired."""


class UploadGrantLimitError(RuntimeError):
    """The caller already holds its maximum number of live upload grants."""


class UploadRequest(FrozenModel):
    """One declared original a caller intends to upload as preserved memory."""

    filename: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    media_type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    size: Annotated[int, Field(gt=0)]
    sha256: Sha256Hex
    scopes: ScopeNames | None = None
    companion_text: str | None = None

    @model_validator(mode="after")
    def within_deployment_bounds(self) -> Self:
        """Enforce the live deployment limits at validation time instead of import time."""
        if self.scopes is not None and len(self.scopes) > settings.mcp_scope_names_max:
            raise ValueError(f"scopes must name at most {settings.mcp_scope_names_max} entries")
        companion = self.companion_text or ""
        if len(companion) > settings.web_artifact_companion_max_chars:
            raise ValueError(
                "companion_text must be at most"
                f" {settings.web_artifact_companion_max_chars} characters"
            )
        return self

    @model_validator(mode="after")
    def safe_identity(self) -> Self:
        """Reject unsafe names now with the exact validation the conversion service applies."""
        ArtifactBytes(content=b"", filename=self.filename, media_type=self.media_type)
        return self


class UploadGrant(FrozenModel):
    """One live single-use capability PUT URL and its remaining lifetime."""

    url: str
    expires_seconds: int


class UploadTicket(FrozenModel):
    """One claimed capability bound to its verified caller and declared file."""

    user: User
    declared: UploadRequest


class TicketRecord(FrozenModel):
    """Persisted caller standing and declaration one claimed capability restores."""

    user_id: UUID5
    name: str | None = None
    username: str | None = None
    avatar: str | None = None
    roles: tuple[str, ...] = ()
    scopes: ScopeTable
    organizations: tuple[OrganizationStanding, ...] = ()
    declared: UploadRequest

    @field_serializer("organizations")
    def serialize_full_standing(
        self, organizations: tuple[OrganizationStanding, ...]
    ) -> list[dict[str, JsonValue]]:
        """Persist each standing with the scope ID its directory serialization excludes."""
        return [
            {"id": str(organization.id)} | organization.model_dump(mode="json")
            for organization in organizations
        ]

    @classmethod
    def pack(cls, user: User, declared: UploadRequest) -> Self:
        """Snapshot the verified caller and declaration for the redeeming process."""
        return cls(
            user_id=user.id,
            name=user.name,
            username=user.username,
            avatar=user.avatar,
            roles=user.roles,
            scopes=user.scopes,
            organizations=user.organizations,
            declared=declared,
        )

    def restore(self) -> UploadTicket:
        """Rebuild the verified caller exactly as the minting process resolved it."""
        return UploadTicket(
            user=User(
                id=self.user_id,
                name=self.name,
                username=self.username,
                avatar=self.avatar,
                roles=self.roles,
                scopes=self.scopes,
                organizations=self.organizations,
            ),
            declared=self.declared,
        )


async def gather(chunks: AsyncIterable[bytes], budget: int) -> bytes:
    """Accumulate one request body while refusing to exceed its byte budget."""
    received = bytearray()
    async for chunk in chunks:
        if len(received) + len(chunk) > budget:
            raise ByteLimitExceeded("the upload exceeds its declared byte budget")
        received.extend(chunk)
    return bytes(received)


class UploadBox(FlexModel):
    """Mint and redeem single-use short-TTL upload capabilities through PostgreSQL.

    Grants persist in the `upload_capability` table under the system scope, so a
    capability minted by the MCP server process is redeemable by the separate API
    service and by any replica of either. A claim consumes its row atomically and
    minting holds every caller to a small live-grant cap.
    """

    intake: Intake
    upload_byte_limit: int = Field(default_factory=lambda: settings.object_store_upload_byte_limit)
    ttl_seconds: float = Field(default_factory=lambda: float(settings.api_upload_ttl_seconds))
    live_grants_per_caller: int = Field(
        default_factory=lambda: settings.api_upload_live_grants_per_caller
    )
    api_base_url: str = Field(default_factory=lambda: settings.api_base_url)

    @classmethod
    def from_settings(cls, config: Settings, intake: Intake) -> Self:
        """Build the box over one intake with every bound taken from explicit settings."""
        return cls(
            intake=intake,
            upload_byte_limit=config.object_store_upload_byte_limit,
            ttl_seconds=float(config.api_upload_ttl_seconds),
            live_grants_per_caller=config.api_upload_live_grants_per_caller,
            api_base_url=config.api_base_url,
        )

    async def deliver(self, ticket: UploadTicket, content: bytes) -> ArtifactReceipt:
        """Run one claimed upload through the malware-scanned secure intake path."""
        if len(content) != ticket.declared.size:
            raise ValueError("the upload does not match its declared byte size")
        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash != ticket.declared.sha256:
            raise ValueError("the upload does not match its declared content hash")
        return await self.intake.accept(
            ticket.user,
            ArtifactBytes(
                content=content,
                filename=ticket.declared.filename,
                media_type=ticket.declared.media_type,
            ),
            scopes=ticket.declared.scopes,
            companion_text=ticket.declared.companion_text,
        )

    async def mint(self, user: User, declared: UploadRequest) -> UploadGrant:
        """Authorize the declaration now and mint one bounded single-use capability."""
        if declared.size > self.upload_byte_limit:
            raise ValueError(f"size must be less than or equal to {self.upload_byte_limit}")
        user.write_scope(declared.scopes)
        record = TicketRecord.pack(user, declared)
        capability = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        async with User.system() as session:
            await session.exec(
                select(func.pg_advisory_xact_lock(func.hashtextextended(str(user.id), 0)))
            )
            await session.exec(delete(UploadCapability).where(UploadCapability.expires_at < now))
            live = (
                await session.exec(
                    select(func.count())
                    .select_from(UploadCapability)
                    .where(UploadCapability.created_by == user.id)
                )
            ).one()
            if live >= self.live_grants_per_caller:
                raise UploadGrantLimitError(
                    "too many live upload grants, wait for one to expire or be used"
                )
            session.add(
                UploadCapability(
                    capability=capability,
                    created_by=user.id,
                    scopes=[settings.system_user_id],
                    ticket=record.model_dump(mode="json"),
                    expires_at=now + timedelta(seconds=self.ttl_seconds),
                )
            )
        return UploadGrant(
            url=f"{self.api_base_url}/api/uploads/{capability}",
            expires_seconds=round(self.ttl_seconds),
        )

    async def claim(self, capability: str) -> UploadTicket:
        """Consume one capability exactly once while it is still live."""
        async with User.system() as session:
            row = (
                await session.exec(
                    delete(UploadCapability)
                    .where(UploadCapability.capability == capability)
                    .returning(UploadCapability.ticket, UploadCapability.expires_at)
                )
            ).first()
        if row is None:
            raise UploadCapabilityError("upload capability is unknown or already used")
        ticket, expires_at = row
        if expires_at < datetime.now(UTC):
            raise UploadCapabilityError("upload capability expired")
        return TicketRecord.model_validate(ticket).restore()
