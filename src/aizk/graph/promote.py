import uuid
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from enum import StrEnum, auto
from typing import cast

from loguru import logger
from patos import FrozenModel
from pydantic import UUID5, UUID7
from sqlalchemy import delete
from sqlalchemy.orm import QueryableAttribute, selectinload
from sqlmodel import select

from ..exceptions import NotVisibleError
from ..store import Artifact, Chunk, Document, Fact
from ..store.engine import Session
from ..store.identity import User
from ..store.locking import acquire_locks, document_revision
from ..store.models.views import LiveFact
from ..types import Scopes
from .dedupe import claim_entities, claim_facts


class Promotion(FrozenModel):
    """One source document and the target-scope copy that now stands for it."""

    class Outcome(StrEnum):
        """What one promotion had to do before the copy stood for the source."""

        created = auto()
        refreshed = auto()
        current = auto()

    source: UUID7
    destination: UUID7
    outcome: Outcome

    @property
    def changed(self) -> bool:
        """Whether this promotion wrote the destination rather than finding it current."""
        return self.outcome is not Promotion.Outcome.current


class Promoter:
    """Carry visible documents into one destination scope set as provenance-linked copies.

        sources + standing copies + live facts, batched
                            |
            current? -- reuse | stale? -- refresh | absent? -- create
                            |
                grounded chunks, artifact metadata and claims

    A destination copy is authoritative only while it carries the source's current content,
    so the carry compares content hashes rather than trusting that a copy exists. A source
    revised after an earlier share therefore refreshes its copy instead of silently leaving
    the old one, which is what keeps a move from retiring a source whose destination still
    holds the previous text.
    """

    __slots__ = ("session", "target", "user_id")

    def __init__(self, session: Session, target: list[UUID5], user_id: UUID5) -> None:
        self.session = session
        self.target = target
        self.user_id = user_id

    async def carry(self, document_ids: Collection[UUID7]) -> list[Promotion]:
        """Bring every named source's destination copy up to date, in one batch.

        Locks are taken in one global order, every document before every artifact and each
        family sorted inside its own call, so two batches that overlap on either resource
        queue behind each other instead of each holding what the other waits for.
        """
        await acquire_locks(self.session, [document_revision(source) for source in document_ids])
        sources = await self.sources(document_ids)
        await acquire_locks(self.session, self.artifact_keys(sources.values()))
        standing = await self.standing_copies(sources)
        facts = await self.live_facts(sources.values())
        now = datetime.now(UTC)
        promotions: list[Promotion] = []
        for document_id in dict.fromkeys(document_ids):
            source = sources[document_id]
            grounding = facts[document_id]
            copy = standing.get(document_id)
            if copy is None:
                promotions.append(
                    Promotion(
                        source=document_id,
                        destination=await self.create(source, grounding),
                        outcome=Promotion.Outcome.created,
                    )
                )
                continue
            if self.stands_for(copy, source, now):
                promotions.append(
                    Promotion(
                        source=document_id,
                        destination=copy.id,
                        outcome=Promotion.Outcome.current,
                    )
                )
                continue
            await self.refresh(copy, source, grounding, now)
            promotions.append(
                Promotion(
                    source=document_id,
                    destination=copy.id,
                    outcome=Promotion.Outcome.refreshed,
                )
            )
        logger.info(
            "shared {} documents into {}",
            sum(promotion.changed for promotion in promotions),
            self.target,
        )
        return promotions

    @staticmethod
    def artifact_keys(sources: Collection[Document]) -> list[str]:
        """One lock per stored original a batch will reach, keyed by the artifact itself.

        Sharing artifact metadata takes its own finer lock per document. Claiming the whole
        batch's artifacts here first, in one sorted call, means every promoter queues on the
        same keys in the same order before it reaches that finer lock, so two batches that
        touch the same originals in opposite document order cannot deadlock.
        """
        return [
            f"artifact|{source.artifact_id}"
            for source in sources
            if source.artifact_id is not None
        ]

    @staticmethod
    def stands_for(copy: Document, source: Document, moment: datetime) -> bool:
        """Whether a standing copy still represents the source, so a share may leave it alone.

        The content hash answers the ordinary question. Activity answers the awkward one: a
        copy retired on its own while its source still holds is no destination for a move
        about to retire that source, yet a copy retired because the source itself was already
        moved must stay untouched, or repeating a move would stamp the source's tombstone
        expiry onto the copy and erase the destination.
        """
        return copy.content_hash == source.content_hash and (
            copy.active_at(moment) or not source.active_at(moment)
        )

    @staticmethod
    def inherited_expiry(copy: Document, source: Document, moment: datetime) -> datetime | None:
        """The expiry a refreshed copy takes from its source, unless taking it would retire it.

        An ordinary source expiry is a validity boundary the copy should carry. A retired
        source's expiry is a tombstone instead, and a source whose content moved on after it
        was retired would otherwise stamp that tombstone onto a destination this very refresh
        just brought up to date. A live copy therefore keeps its own expiry whenever the
        source it is catching up to is already retired.
        """
        if source.active_at(moment) or not copy.active_at(moment):
            return source.expires_at
        return copy.expires_at

    async def sources(self, document_ids: Collection[UUID7]) -> dict[UUID7, Document]:
        """Every named visible source with its ordered chunks, loaded in one statement."""
        chunks = cast(QueryableAttribute[list[Chunk]], Document.chunks)
        rows = await self.session.exec(
            select(Document).where(Document.id.in_(document_ids)).options(selectinload(chunks))
        )
        found = {source.id: source for source in rows}
        if missing := [document_id for document_id in document_ids if document_id not in found]:
            raise NotVisibleError(f"no visible document {missing[0]}")
        self.reject_quarantined(found.values())
        return found

    @staticmethod
    def reject_quarantined(sources: Collection[Document]) -> None:
        """Refuse to carry a cached web page into any other scope.

        A cached page is a stranger's text held only so the next question is cheaper. It is
        quarantined where it landed, out of the graph and under the web label, and a copy is
        a new document that would have to earn that quarantine all over again. Promotion of
        it is not meaningful either, since anyone in the destination can reach the same page
        by asking, so the honest answer is to refuse rather than to carry a page across a
        boundary it was never allowed to cross.
        """
        quarantined = [source for source in sources if source.origin is Document.Origin.web_cache]
        if not quarantined:
            return
        named = ", ".join(sorted(str(source.id) for source in quarantined))
        raise ValueError(
            f"{len(quarantined)} of the named documents are cached web pages rather than "
            "your own notes, and a cached page is never shared. Anyone in the destination "
            "can find the same page themselves, so keep what you concluded from it "
            f"instead and share that: {named}"
        )

    async def standing_copies(self, sources: dict[UUID7, Document]) -> dict[UUID7, Document]:
        """The copies earlier shares already promoted into the target, keyed by their source."""
        rows = await self.session.exec(
            select(Document).where(
                Document.promoted_from.in_(sources), Document.scopes == self.target
            )
        )
        return {cast(UUID7, copy.promoted_from): copy for copy in rows}

    async def live_facts(self, sources: Collection[Document]) -> dict[UUID7, list[LiveFact]]:
        """Each source's own live facts, gathered for every source in one statement."""
        origin = {chunk.id: source.id for source in sources for chunk in source.chunks}
        grouped: dict[UUID7, list[LiveFact]] = {source.id: [] for source in sources}
        rows = await self.session.exec(
            select(Fact.Live).where(Fact.Live.source_chunk_id.in_(origin))
        )
        for fact in rows:
            grouped[origin[cast(UUID7, fact.source_chunk_id)]].append(fact)
        return grouped

    async def create(self, source: Document, facts: list[LiveFact]) -> UUID7:
        """Insert one fresh copy of a source in the destination scope set."""
        destination = uuid.uuid7()
        artifact_id, artifact_content_id = await Artifact.share(
            self.session, source, self.user_id, self.target
        )
        self.session.add(
            Document(
                id=destination,
                title=source.title,
                subject_type=source.subject_type,
                source_uri=source.source_uri,
                observed_at=source.observed_at,
                expires_at=source.expires_at,
                artifact_id=artifact_id,
                artifact_content_id=artifact_content_id,
                content_hash=source.content_hash,
                origin=source.origin,
                created_by=self.user_id,
                scopes=self.target,
                promoted_from=source.id,
            )
        )
        await self.session.flush()
        await self.ground(source, destination, facts)
        return destination

    async def refresh(
        self, copy: Document, source: Document, facts: list[LiveFact], moment: datetime
    ) -> None:
        """Bring a stale destination copy back onto the source's current content.

        The copy keeps its identity, because the destination's unique title and source
        locator leave no room for a second generation beside it. Its claims close with a
        `refreshed` marker and its chunks give way to the source's current spans, so nothing
        the destination once said is deleted, it simply stops being live. Only its expiry is
        taken conditionally, since a refresh must never be the thing that retires a copy.
        """
        await Fact.Claim.retract_from_documents(self.session, [copy.id], "refreshed")
        await self.session.exec(delete(Chunk).where(Chunk.document_id == copy.id))
        artifact_id, artifact_content_id = await Artifact.share(
            self.session, source, self.user_id, self.target
        )
        copy.title = source.title
        copy.subject_type = source.subject_type
        copy.source_uri = source.source_uri
        copy.observed_at = source.observed_at
        copy.expires_at = self.inherited_expiry(copy, source, moment)
        copy.artifact_id = artifact_id
        copy.artifact_content_id = artifact_content_id
        copy.content_hash = source.content_hash
        copy.origin = source.origin
        self.session.add(copy)
        await self.session.flush()
        await self.ground(source, copy.id, facts)

    async def ground(self, source: Document, destination: UUID7, facts: list[LiveFact]) -> None:
        """Give one destination copy the source's chunks and re-claim the graph they ground."""
        copies = {
            chunk.id: Chunk(
                document_id=destination,
                ord=chunk.ord,
                text=chunk.text,
                lexical=chunk.lexical,
                tokens=chunk.tokens,
                provenance=dict(chunk.provenance),
                embedding=chunk.embedding,
                processed_at=chunk.processed_at,
                created_by=self.user_id,
                scopes=self.target,
            )
            for chunk in source.chunks
        }
        self.session.add_all(copies.values())
        await self.session.flush()
        await claim_entities(
            self.session,
            {fact.subject_id for fact in facts}
            | {fact.object_id for fact in facts if fact.object_id is not None},
            self.user_id,
            self.target,
        )
        await claim_facts(
            self.session,
            [
                {
                    "content_id": fact.content_id,
                    "valid_from": fact.valid_from,
                    "valid_to": fact.valid_to,
                    "source_chunk_id": copies[cast(UUID7, fact.source_chunk_id)].id,
                    "attributes": dict(fact.attributes),
                    "perspective_key": fact.perspective_key,
                    "promoted_from": fact.id,
                }
                for fact in facts
            ],
            self.user_id,
            self.target,
        )


async def promote(document_ids: Collection[UUID7], scopes: Scopes, user: User) -> list[Promotion]:
    """Share visible documents into one authorized scope set as provenance-linked copies."""
    async with user as session:
        return await Promoter(session, sorted(scopes), user.id).carry(document_ids)


async def transfer(
    document_ids: Collection[UUID7], scopes: Scopes, user: User, move: bool = False
) -> list[Promotion]:
    """Copy documents into one destination and, on a move, retire the originals with them.

    A move is a copy followed by a retirement rather than a scope rewrite in place. The
    rewrite is not available: a chunk inherits visibility through its document, so row
    security checks the pair `(document_id, scopes)` against the parent on every chunk
    update, and no ordering of a two-table rewrite satisfies that check on both the old and
    the new row. Both halves therefore run inside one transaction, so a move can never commit
    a copy and then lose the retirement to a crash, which would leave the same knowledge live
    in two scopes with no record that anything was owed. What retires is the source's live
    claims, closed with a `moved` marker that keeps their history, and the document itself,
    expired so recall stops returning it while its rows, bytes and provenance chain stay.
    """
    async with user as session:
        promotions = await Promoter(session, sorted(scopes), user.id).carry(document_ids)
        if move:
            await retire(session, [promotion.source for promotion in promotions])
    return promotions


async def retire(session: Session, document_ids: Sequence[UUID7]) -> list[UUID7]:
    """Close the private originals a move leaves behind, the second half of a transfer."""
    await Fact.Claim.retract_from_documents(session, list(document_ids), "moved")
    retired = await Document.retire(session, document_ids)
    logger.info("moved {} documents out of their original scope", len(retired))
    return retired
