from datetime import UTC, datetime, timedelta

from loguru import logger
from patos import FrozenFlexModel

from ..config import Settings
from ..extract.ingest import TextIngestor, TextSource
from ..integrations.clamav import (
    ContentScanner,
    MalwareRejectedError,
    MalwareUnavailableError,
)
from ..integrations.web import Freshness, cache_days
from ..provenance import CaptureContext
from ..store import Document
from ..store.identity import User
from ..types import Scopes
from .models import WebFinding


class WebCache(FrozenFlexModel):
    """Keep a fetched page as an ordinary document so the next question is free.

    There is no second store and no second index. A cached page is a document with the
    `web_cache` origin, which puts it in the same lanes, under the same row security, with
    the same dedup and the same in-place refresh every other source gets. That is the whole
    economy of it, since the sufficiency signal the router reads is ordinary retrieval and
    therefore already sees what an earlier call paid for.

    Three rules keep the convenience from becoming contamination, and all three follow from
    the origin marker rather than from anyone remembering them. The page is never enqueued
    for graph projection and the recovery sweep skips it, so no third-party claim ever
    becomes an entity, a fact, a profile, a community or an insight. It renders under the
    web provenance label wherever it surfaces, so it can never read as something the caller
    wrote. And it carries an expiry from the freshness the planner assigned, so a stale page
    leaves retrieval on its own rather than answering for a world that moved on.
    """

    scanner: ContentScanner
    stable_days: int
    dated_days: int
    volatile_days: int

    @classmethod
    def build(cls, config: Settings, scanner: ContentScanner) -> WebCache:
        """Bind the deployment's scanner and freshness lifetimes into one writer."""
        return cls(
            scanner=scanner,
            stable_days=cache_days(config, Freshness.stable),
            dated_days=cache_days(config, Freshness.dated),
            volatile_days=cache_days(config, Freshness.volatile),
        )

    def expiry(self, freshness: Freshness) -> datetime:
        """When a page of this freshness stops being allowed to answer."""
        days = {
            Freshness.stable: self.stable_days,
            Freshness.dated: self.dated_days,
            Freshness.volatile: self.volatile_days,
        }[freshness]
        return datetime.now(UTC) + timedelta(days=days)

    async def keep(
        self,
        user: User,
        findings: tuple[WebFinding, ...],
        freshness: Freshness,
        scopes: Scopes,
    ) -> tuple[WebFinding, ...]:
        """Store every page whose provider licensed it, and return what survived the scan.

        A finding a provider marked unpersistable is returned to the caller and never
        written, so a vendor's display-only rows cannot become part of anyone's memory by
        passing through this function.

        The two scanner outcomes are deliberately different. A page the scanner calls
        malicious is dropped from the answer as well as from the store, because such content
        is not evidence. A page the scanner could not reach a verdict on is still answered
        with, since the caller already holds it and hiding it would help nobody, but it is
        not written, because storing unscanned bytes is the thing the scan exists to prevent.
        """
        expires_at = self.expiry(freshness)
        kept: list[WebFinding] = []
        for finding in findings:
            if not finding.persistable:
                kept.append(finding)
                continue
            try:
                await self.scanner.scan(finding.text.encode("utf-8"))
            except MalwareRejectedError as rejected:
                logger.warning("dropped a fetched page the scanner rejected: {}", rejected)
                continue
            except MalwareUnavailableError as unavailable:
                logger.warning("left a fetched page uncached, scanning is down: {}", unavailable)
                kept.append(finding)
                continue
            kept.append(finding)
            await self.store(user, finding, expires_at, scopes)
        return tuple(kept)

    async def store(
        self,
        user: User,
        finding: WebFinding,
        expires_at: datetime,
        scopes: Scopes,
    ) -> None:
        """Write one page, treating a storage failure as a page that simply was not cached.

        Caching is an optimisation for the next question. The answer to this one is already
        in hand, and letting a page the store would not take destroy the memory half of a
        `find` would trade the whole answer for a saving nobody asked for. A crafted page
        can reach real validation failures down in ingestion, so this is the boundary where
        those stop being the caller's problem.
        """
        try:
            await self.write(user, finding, expires_at, scopes)
        except ValueError as refused:
            logger.warning(
                "left a fetched page uncached, the store refused it, {}", type(refused).__name__
            )

    @staticmethod
    async def write(
        user: User,
        finding: WebFinding,
        expires_at: datetime,
        scopes: Scopes,
    ) -> None:
        """Write one page through the ordinary ingestion path, never enqueueing projection."""
        document_id, _ = await TextIngestor(user).ingest(
            TextSource(
                text=finding.text,
                title=finding.title or str(finding.url),
                source_uri=Document.cache_locator(str(finding.url)),
                created_by=user.id,
                scopes=scopes,
                origin=Document.Origin.web_cache,
                capture=CaptureContext(expires_at=expires_at),
            )
        )
        logger.info("cached web page {} as document {}", finding.url, document_id)
