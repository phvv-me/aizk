from datetime import datetime
from typing import cast

from patos import FrozenModel
from pydantic import Field

from . import graph, retrieval
from .artifacts import ArtifactIntake, ArtifactReceipt
from .background.jobs.projection import enqueue_document
from .background.wake import NoopWorkerWake, WorkerWake
from .extract import ingest as extract_ingest
from .provenance import CaptureContext
from .retrieval import Evidence, RecallEvidence, RecallResult
from .store import Document, Usage
from .store.identity import User
from .types import UUID7, ScopeNames, Scopes
from .usage import annotate_operation, quota
from .web import Refusal, WebFinding, WebMode, WebOutcome, WebSearch


class WriteResult(FrozenModel):
    """Identify the durable source document created or updated by `keep`."""

    id: UUID7


class SelectedDocument(FrozenModel):
    """One candidate source with the scope set that decides whether a share may carry it."""

    id: UUID7
    title: str | None = None
    scopes: Scopes = frozenset()


class SharedDocument(FrozenModel):
    """One document a share carried, named so the caller can verify what happened."""

    id: UUID7
    title: str | None = None
    destination: UUID7 | None = Field(
        default=None, description="the copy standing in the target scope, absent in a preview"
    )


class ShareResult(FrozenModel):
    """Exactly which documents one share carried, how, and whether it only looked."""

    documents: tuple[SharedDocument, ...] = ()
    moved: bool = False
    preview: bool = False


class Memory:
    """Expose AIZK memory operations for one authenticated caller.

    MCP and web transports share this service. Identity resolution and input size
    limits stay at each transport boundary while retrieval, ingestion, scope
    authorization, and graph projection remain defined once here. The transport
    constructs one per request with the caller and the process artifact intake.
    """

    __slots__ = ("intake", "user", "wake", "web")

    def __init__(
        self,
        user: User,
        intake: ArtifactIntake,
        wake: WorkerWake | None = None,
        web: WebSearch | None = None,
    ) -> None:
        self.user = user
        self.intake = intake
        self.wake = wake or NoopWorkerWake()
        self.web = web

    @property
    def status(self) -> User:
        """Return the caller and its current Logto-derived authority."""
        return self.user

    async def recall(self, query: str, budget: int) -> RecallResult:
        """Return structured merit-ordered evidence visible to this caller."""
        _, result = await self.remembered(query, budget)
        return result

    async def remembered(self, query: str, budget: int) -> tuple[RecallEvidence, RecallResult]:
        """Run the memory half of a question, keeping the signals an egress router reads."""
        await quota.consume(self.user.id, Usage.Event.Operation.recall)
        evidence = await retrieval.evidence(query.strip(), self.user, token_budget=budget)
        candidates = evidence.candidates
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
        return evidence, RecallResult.from_candidates(candidates, scope_details)

    async def find(
        self,
        query: str,
        budget: int,
        scopes: ScopeNames | None = None,
        web: WebMode = WebMode.auto,
        fresh: bool = False,
    ) -> RecallResult:
        """Answer one question from memory, and from the public web when memory cannot.

            memory retrieval, always, free
                    |
            router and sanitizer, reading what memory already produced
                    |
            the public web, only for what survived both
                    |
            fetched pages cached as ordinary expiring documents
                    |
            one receipt naming exactly what left the machine

        Memory always runs and always renders first, so a call that also reached the web
        answers as both halves rather than as the web alone. A deployment with no web
        service wired refuses in the same shape a disabled one does, which keeps the
        receipt honest on every path.
        """
        evidence, result = await self.remembered(query, budget)
        outcome = (
            WebOutcome.refused(Refusal.not_permitted)
            if self.web is None
            else await self.web.run(self.user, query.strip(), evidence, web, fresh, scopes)
        )
        return result.model_copy(
            update={
                "web": self.web_evidence(outcome.findings),
                "receipt": outcome.receipt,
            }
        )

    @staticmethod
    def web_evidence(findings: tuple[WebFinding, ...]) -> tuple[Evidence, ...]:
        """Render fetched pages as evidence carrying their provider and retrieval date."""
        return tuple(
            RecallResult.Evidence(
                provenance=RecallResult.Provenance.WEB,
                text=finding.text,
                provider=finding.provider,
                retrieved_at=finding.retrieved_at,
                source_url=str(finding.url),
            )
            for finding in findings
        )

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

    async def share(
        self,
        documents: list[UUID7] | None = None,
        query: str | None = None,
        scopes: ScopeNames | None = None,
        move: bool = False,
        limit: int = 20,
        preview: bool = False,
    ) -> ShareResult:
        """Copy or move named documents into one authorized destination, or preview a query.

            query -> selection -> preview, always
                                     |
                    the caller reads it and approves ids
                                     |
            documents -> selection -> provenance-linked copies
                                     |
                          retire the originals on a move

        Either explicit `documents` or one `query` names the sources, never both, and only
        explicit documents are ever written. A query answers what it would select and stops
        there, because a question matches on ranked similarity rather than on the caller's
        intent, and a phrase that happens to reach a dozen private notes must not be able to
        hand them to an organization on one call. Sharing therefore takes two steps, a query
        to see the candidates and a second call naming the ids the caller approved.

        `preview` is what the caller asks for in the second step, since previewing an exact
        list is a real question. Alongside a query it is redundant and simply agrees with
        what already happens. A move alongside a query is refused rather than quietly
        ignored, because dropping a stated intent to write would leave the caller believing
        documents had moved when nothing did.

        A query selection and a move both read only the caller's own private documents, so
        both demand an organization destination. Carrying private documents into the private
        scope they already occupy would copy a scope onto itself and breed one generation per
        repeat, which is a mistake worth naming rather than a no-op worth allowing.
        """
        if (documents is None) == (query is None):
            raise ValueError("share takes either explicit documents or one selection query")
        if query is not None and move:
            raise ValueError(
                "a selection query only ever previews, so it cannot move. Read the query's "
                "candidates, then call again with the document IDs you approve and move those"
            )
        await quota.consume(self.user.id, Usage.Event.Operation.share)
        target = self.user.write_scope(scopes)
        personal = documents is None or move
        if personal and target == frozenset({self.user.id}):
            raise ValueError(
                "a selection query and a move both carry your own private documents, "
                "so both need an organization destination"
            )
        previewing = preview or query is not None
        selected = await self.selection(documents, query, personal, target, limit)
        if previewing or not selected:
            return ShareResult(documents=selected, moved=move, preview=previewing)
        promotions = {
            promotion.source: promotion.destination
            for promotion in await graph.transfer(
                [item.id for item in selected], target, self.user, move
            )
        }
        annotate_operation(Usage.Event.Operation.share, target, len(promotions))
        return ShareResult(
            documents=tuple(
                item.model_copy(update={"destination": promotions[item.id]}) for item in selected
            ),
            moved=move,
        )

    async def selection(
        self,
        documents: list[UUID7] | None,
        query: str | None,
        personal: bool,
        target: Scopes,
        limit: int,
    ) -> tuple[SharedDocument, ...]:
        """The documents one share will carry, ordered as the caller named or recalled them.

        A document already standing in the destination is dropped rather than carried, since
        the end state the caller asked for already holds and promoting a document into its
        own scope would only breed generations. Explicit IDs are otherwise answered exactly:
        an ID the selection cannot reach is reported, since a caller naming a document it
        cannot share deserves the reason instead of a silent zero. That report names the
        guard that refused and never whether the document exists.
        """
        # A document named twice is still one document and the transfer carries it once, so
        # the selection settles on first-seen order before anything downstream counts it.
        named = list(
            dict.fromkeys(
                documents
                if documents is not None
                else await retrieval.documents(
                    cast("str", query).strip(), self.user, limit, scopes=frozenset({self.user.id})
                )
            )
        )
        rows = await self.user.exec[SelectedDocument](
            Document.shareable(named, self.user.id if personal else None)
        )
        found = {row.id: row for row in rows}
        if documents is not None and (missing := [item for item in named if item not in found]):
            # One message covers a document that does not exist and one this caller cannot
            # reach, deliberately. Telling those apart would answer "does this ID exist" for
            # anyone willing to ask, which is a lookup no caller is entitled to.
            refused = "among your own private documents" if personal else "visible to you"
            raise ValueError(
                f"{len(missing)} named documents are not {refused}: "
                f"{', '.join(str(item) for item in missing)}"
            )
        return tuple(
            SharedDocument(id=row.id, title=row.title)
            for item in named
            if (row := found.get(item)) is not None and row.scopes != target
        )
