from datetime import datetime
from typing import cast

from patos import FrozenModel
from pydantic import UUID7

from . import graph, retrieval
from .artifacts import ArtifactIntake, ArtifactReceipt
from .background.jobs.projection import enqueue_document
from .background.wake import NoopWorkerWake, WorkerWake
from .extract import ingest as extract_ingest
from .provenance import CaptureContext
from .retrieval import RecallResult
from .store import Usage
from .store.identity import User
from .types import ScopeNames
from .usage import annotate_operation, quota


class WriteResult(FrozenModel):
    """Identify the durable source document created or updated by `remember`."""

    id: UUID7


class ShareResult(FrozenModel):
    """Report how many provenance-linked document copies `share` created."""

    shared: int


class Memory:
    """Expose AIZK memory operations for one authenticated caller.

    MCP and web transports share this service. Identity resolution and input size
    limits stay at each transport boundary while retrieval, ingestion, scope
    authorization, and graph projection remain defined once here. The transport
    constructs one per request with the caller and the process artifact intake.
    """

    __slots__ = ("intake", "user", "wake")

    def __init__(
        self,
        user: User,
        intake: ArtifactIntake,
        wake: WorkerWake | None = None,
    ) -> None:
        self.user = user
        self.intake = intake
        self.wake = wake or NoopWorkerWake()

    @property
    def status(self) -> User:
        """Return the caller and its current Logto-derived authority."""
        return self.user

    async def recall(self, query: str, budget: int) -> RecallResult:
        """Return structured merit-ordered evidence visible to this caller."""
        await quota.consume(self.user.id, Usage.Event.Operation.recall)
        candidates = await retrieval.recall(query.strip(), self.user, token_budget=budget)
        annotate_operation(
            Usage.Event.Operation.recall,
            frozenset().union(*(candidate.scopes for candidate in candidates)),
            len(candidates),
        )
        scope_details = {self.user.id: RecallResult.Scope(name="private")} | {
            organization.id: RecallResult.Scope(
                name=organization.name,
                description=organization.description,
            )
            for organization in self.user.organizations
        }
        return RecallResult.from_candidates(candidates, scope_details)

    async def remember(
        self,
        text: str | None = None,
        source_uri: str | None = None,
        observed_at: datetime | None = None,
        expires_at: datetime | None = None,
        scopes: ScopeNames | None = None,
        preserve_source: bool = False,
    ) -> WriteResult | ArtifactReceipt:
        """Store text directly or preserve a URI original with optional companion text."""
        if text is None and source_uri is None:
            raise ValueError("remember requires text or a source URI")
        if preserve_source and source_uri is None:
            raise ValueError("preserve_source requires a source URI")
        operation = (
            Usage.Event.Operation.remember_file
            if source_uri is not None and (text is None or preserve_source)
            else Usage.Event.Operation.remember_text
        )
        await quota.consume(self.user.id, operation)
        if source_uri is not None and (text is None or preserve_source):
            result = await self.intake.uri(
                self.user,
                source_uri,
                scopes=scopes,
                companion_text=text,
                observed_at=observed_at,
                expires_at=expires_at,
            )
            await self.wake.wake()
            return result
        text = cast("str", text)
        declaration = extract_ingest.SourceDeclaration.from_text(text)
        target = self.user.write_scope(scopes)
        annotate_operation(Usage.Event.Operation.remember_text, target)
        document_id = await extract_ingest.ingest_text(
            self.user,
            text,
            title=declaration.title,
            source_uri=source_uri,
            created_by=self.user.id,
            scopes=target,
            capture=CaptureContext(
                speaker_label=self.user.label,
                observed_at=observed_at,
                expires_at=expires_at,
            ),
        )
        if document_id is None:
            raise ValueError("memory ingestion did not create a document")
        await enqueue_document(document_id, target)
        await self.wake.wake()
        return WriteResult(id=document_id)

    async def share(self, documents: list[UUID7], scopes: ScopeNames | None = None) -> ShareResult:
        """Copy visible documents into one authorized destination without moving sources."""
        await quota.consume(self.user.id, Usage.Event.Operation.share)
        target = self.user.write_scope(scopes)
        shared = await graph.promote(documents, target, self.user)
        annotate_operation(Usage.Event.Operation.share, target, shared)
        return ShareResult(shared=shared)
