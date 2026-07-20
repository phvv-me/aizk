from datetime import UTC, datetime
from typing import cast

from pydantic import UUID7, JsonValue
from sqlalchemy import or_
from sqlmodel import select

from ..storage import IntegrityCheck, StoredBytes, StoredObject
from ..store import Artifact, Blob
from ..store.identity import User
from ..store.models.tables import ArtifactContent
from ..types import Scopes
from .models import ArtifactReceipt, OriginalArtifact, OriginalDescription


def _postgres_json(value: JsonValue) -> JsonValue:
    """Replace only NUL code points that PostgreSQL JSONB cannot represent."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, list):
        return [_postgres_json(item) for item in value]
    if isinstance(value, dict):
        return {key.replace("\x00", "\ufffd"): _postgres_json(item) for key, item in value.items()}
    return value


class ArtifactRepository:
    """Persist artifact metadata through exact caller-bound PostgreSQL transactions."""

    async def create_original(
        self,
        user: User,
        stored: StoredBytes,
        described: OriginalDescription,
        scopes: Scopes,
    ) -> ArtifactReceipt:
        """Create a logical artifact revision that references one immutable stored object."""
        ordered_scopes = sorted(scopes, key=str)
        async with user as session:
            artifact = None
            if described.source_uri is not None:
                artifact = (
                    await session.exec(
                        select(Artifact).where(
                            Artifact.source_uri == described.source_uri,
                            Artifact.scopes == ordered_scopes,
                        )
                    )
                ).first()
            if artifact is None:
                artifact = Artifact(
                    name=described.filename,
                    source_uri=described.source_uri,
                    created_by=user.id,
                    scopes=ordered_scopes,
                )
                session.add(artifact)
                await session.flush()
            else:
                artifact.name = described.filename
            revision = (
                await session.exec(
                    select(Artifact.Content.revision.max(default=0)).where(
                        Artifact.Content.artifact_id == artifact.id
                    )
                )
            ).one() + 1
            blob = Blob(
                **stored.model_dump(by_alias=True),
                media_type=described.media_type,
            )
            session.add(blob)
            await session.flush()
            content = Artifact.Content(
                **described.model_dump(
                    exclude={"filename", "media_type", "source_uri"},
                ),
                artifact_id=artifact.id,
                blob_id=blob.id,
                revision=revision,
                created_by=user.id,
                scopes=ordered_scopes,
            )
            session.add(content)
            await session.flush()
            return ArtifactReceipt(
                artifact_id=artifact.id,
                content_id=content.id,
                state=content.state,
            )

    async def pending(self, user: User, scopes: Scopes, limit: int) -> tuple[UUID7, ...]:
        """Return bounded pending originals in one exact scope for autonomous dispatch."""
        async with user as session:
            rows = await session.exec(
                select(Artifact.Content.id)
                .where(
                    Artifact.Content.state == Artifact.Content.State.pending,
                    Artifact.Content.scopes == sorted(scopes, key=str),
                )
                .order_by(Artifact.Content.created_at, Artifact.Content.id)
                .limit(limit)
            )
            return tuple(rows)

    async def integrity_candidates(
        self,
        stale_before: datetime,
        limit: int,
    ) -> tuple[StoredObject, ...]:
        """Load failed, unverified, or stale object references for one bounded system pass."""
        async with User.system().owner as session:
            rows = (
                await session.exec(
                    select(Blob)
                    .where(
                        or_(
                            Blob.integrity_error.is_not(None),
                            Blob.integrity_checked_at.is_(None),
                            Blob.integrity_checked_at < stale_before,
                        )
                    )
                    .order_by(
                        Blob.integrity_error.is_not(None).desc(),
                        Blob.integrity_checked_at.asc().nulls_first(),
                        Blob.created_at,
                        Blob.id,
                    )
                    .limit(limit)
                )
            ).all()
        return tuple(
            StoredObject(
                id=row.id,
                key=row.storage_key,
                content_hash=row.content_hash,
                size=row.size,
                encoding=row.encoding,
                version=row.storage_version,
            )
            for row in rows
        )

    async def record_integrity(
        self,
        checks: tuple[IntegrityCheck, ...],
        checked_at: datetime,
    ) -> None:
        """Record one pass in one owner transaction without changing immutable metadata."""
        if not checks:
            return
        errors = {check.id: check.error for check in checks}
        async with User.system().owner as session:
            rows = (await session.exec(select(Blob).where(Blob.id.in_(errors)))).all()
            if len(rows) != len(checks):
                raise LookupError("an integrity candidate disappeared before recording")
            for row in rows:
                row.integrity_checked_at = checked_at
                row.integrity_error = errors[row.id]

    async def original(
        self,
        user: User,
        content_id: UUID7,
        scopes: Scopes,
    ) -> OriginalArtifact:
        """Load one visible original and reject a stale or forged queue scope set."""
        async with user as session:
            content = await session.get(Artifact.Content, content_id)
            if content is None:
                raise LookupError("artifact original is not visible")
            if frozenset(content.scopes) != scopes:
                raise PermissionError("artifact queue scopes do not match the stored original")
            artifact = (
                await session.exec(select(Artifact).where(Artifact.id == content.artifact_id))
            ).one()
            blob = (await session.exec(select(Blob).where(Blob.id == content.blob_id))).one()
            return OriginalArtifact(
                artifact_id=artifact.id,
                content_id=content.id,
                revision=content.revision,
                created_by=content.created_by,
                scopes=scopes,
                filename=artifact.name,
                media_type=blob.media_type or "application/octet-stream",
                size=blob.size,
                source_uri=artifact.source_uri,
                companion_text=content.companion_text,
                observed_at=content.observed_at,
                expires_at=content.expires_at,
                storage_key=blob.storage_key,
                storage_version=blob.storage_version,
                storage_hash=blob.content_hash,
                storage_encoding=blob.encoding,
            )

    async def set_state(
        self,
        user: User,
        content_id: UUID7,
        scopes: Scopes,
        state: ArtifactContent.State,
        error: str | None = None,
    ) -> None:
        """Advance one visible original while preserving its exact queued scope set."""
        async with user as session:
            content = await session.get(Artifact.Content, content_id)
            if content is None or frozenset(content.scopes) != scopes:
                raise LookupError("artifact original is not visible in its queued scopes")
            content.state = state
            content.error = error
            content.processed_at = (
                datetime.now(UTC)
                if state
                in (
                    Artifact.Content.State.ready,
                    Artifact.Content.State.failed,
                )
                else None
            )

    async def store_conversion(
        self,
        user: User,
        original: OriginalArtifact,
        markdown: str,
        docling_json: dict[str, JsonValue],
        details: dict[str, JsonValue],
    ) -> None:
        """Store replaceable textual and structured derivatives on their exact revision."""
        async with user as session:
            content = await session.get(Artifact.Content, original.content_id)
            if content is None or frozenset(content.scopes) != original.scopes:
                raise LookupError("artifact original is not visible in its conversion scopes")
            content.markdown = markdown
            content.docling_json = cast(
                "dict[str, JsonValue]",
                _postgres_json(docling_json),
            )
            content.details = cast(
                "dict[str, JsonValue]",
                _postgres_json(details),
            )
