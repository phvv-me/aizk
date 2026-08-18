import asyncio
from datetime import UTC, datetime
from time import perf_counter

from loguru import logger
from pydantic.networks import AnyHttpUrl

from ..config import Settings
from ..exceptions import QuotaExceededError
from ..integrations.clamav import ContentScanner
from ..integrations.web import (
    DoclingReader,
    FirecrawlReader,
    Freshness,
    PageConverter,
    PageSource,
    ProviderUnavailable,
    SearchLane,
    WebFetcher,
    WebPage,
    WebResult,
    WebSearcher,
)
from ..retrieval import FindEvidence
from ..serving.extract import LLM
from ..serving.gate import MentionDetector
from ..store.identity import User
from ..store.models.tables import EntityContent, UsageEvent
from ..types import ScopeNames, Scopes
from ..usage import UsageRecorder, capture_step
from ..usage import quota as monthly_quota
from ..usage import recorder as durable_recorder
from .cache import WebCache
from .lanes import lane_providers
from .models import Refusal, RosterName, SanctionedPlan, WebFinding, WebMode, WebOutcome
from .router import RouterProbe, WebRouter
from .sanitizer import QuerySanitizer


class WebSearch:
    """The egress half of `find`, from the first kill switch to the privacy receipt.

    Nothing here is reachable except through `run`, and `run` reads the switches before it
    reads the question. Three must all allow the call and each answers a different one.
    `web_search_enabled` is whether this deployment does egress at all, membership of the
    Logto organization named in `web_search_organization` is whether this person does, and
    the per-call mode is whether this question does. An anonymous caller fails the second by
    construction, so a public read-only visitor gets memory search and nothing more.

    Everything after the switches fails closed. A planner that did not answer, a rewrite the
    sanitizer would not clear, a spent allowance, and a chain where nobody replied all end
    the same way, which is memory alone plus a receipt naming the reason. A web problem is
    never an error the caller sees, because a `find` that could not reach the web still
    answered the question as well as memory could.
    """

    def __init__(
        self,
        config: Settings,
        llm: LLM,
        gate: MentionDetector,
        reader: PageSource,
        converter: PageConverter,
        scanner: ContentScanner,
        recorder: UsageRecorder | None = None,
    ) -> None:
        self.config = config
        self.gate = gate
        self.reader = reader
        self.converter = converter
        self.router = WebRouter(config, llm)
        self.cache = WebCache.build(config, scanner)
        self.recorder = recorder or durable_recorder

    def permitted(self, user: User) -> Refusal | None:
        """Why this caller may not reach the web at all, or nothing when they may."""
        if not self.config.web_search_enabled or user.is_anonymous():
            return Refusal.not_permitted
        if self.config.web_search_organization not in user.organization_ids:
            return Refusal.not_permitted
        return None

    async def roster(self, user: User) -> frozenset[str]:
        """The caller's own private names, lowered and bounded, as the sanitizer checks them.

        Only scopes the caller can write to count. A public organization is readable by
        every account on the deployment, so the names inside it are not this caller's
        private identities, and treating them as such would refuse ordinary public questions
        for everyone at once the moment a public organization stored a common word.

        The list is bounded because it is checked as literal substrings against every
        rewritten query, and an unbounded roster would turn one sanitizer pass into a scan
        over an entire graph.
        """
        writable = frozenset(user.scopes.write)
        rows = await user.exec[RosterName](
            EntityContent.roster(sorted(writable), self.config.web_search_roster_max)
        )
        return frozenset(row.name for row in rows) | {
            organization.name.lower()
            for organization in user.organizations
            if organization.id in writable
        }

    async def run(
        self,
        user: User,
        query: str,
        evidence: FindEvidence,
        mode: WebMode,
        fresh: bool,
        scopes: ScopeNames | None,
    ) -> WebOutcome:
        """Decide, sanitize, spend, fetch, and cache, returning findings and one receipt.

        The whole web half runs under one deadline. A planner and two provider chains are
        three chances to hang, and a `find` that never returns is worse than one that
        answers from memory, so a call that runs out of time degrades exactly as a call
        whose providers refused.
        """
        if mode is WebMode.off:
            return WebOutcome.refused(Refusal.web_off)
        if (barred := self.permitted(user)) is not None:
            return WebOutcome.refused(barred)
        target = user.write_scope(scopes)
        try:
            async with asyncio.timeout(self.config.web_search_deadline):
                return await self.decide(user, query, evidence, mode, fresh, target)
        except TimeoutError:
            logger.warning("the web half of a find ran out of time, answering from memory")
            return WebOutcome.refused(Refusal.provider_failure)

    async def decide(
        self,
        user: User,
        query: str,
        evidence: FindEvidence,
        mode: WebMode,
        fresh: bool,
        target: Scopes,
    ) -> WebOutcome:
        """Read the signals, plan, sanitize, and hand a sanctioned plan to egress."""
        roster = await self.roster(user)
        signals = self.router.signals(
            query, evidence.candidates, evidence.scores, evidence.mentions, roster
        )
        routed = await self.router.plan(
            query,
            signals,
            skip_sufficiency=mode is WebMode.force or fresh,
            skip_roster=mode is WebMode.force,
        )
        if isinstance(routed, Refusal):
            return WebOutcome.refused(routed)
        sanitizer = QuerySanitizer.build(self.config, self.gate, roster)
        if (refused := await sanitizer.refuses(routed.query)) is not None:
            return WebOutcome.refused(refused)
        return await self.egress(user, routed, fresh, target)

    async def egress(
        self, user: User, plan: SanctionedPlan, fresh: bool, target: Scopes
    ) -> WebOutcome:
        """Spend the allowance, run the chains, cache what may be kept, and account for it.

        Whether providers were reached decides which receipt the caller gets. An allowance
        that ran out before the first request kept the question home, while one that ran out
        after two searches did not, and the receipt has to tell those apart or it is not a
        receipt.
        """
        providers: list[str] = []
        try:
            results, searcher = await self.search(user, plan, providers)
        except QuotaExceededError:
            return self.nothing_found(Refusal.quota_exhausted, plan, providers)
        if searcher is None:
            return self.nothing_found(Refusal.provider_failure, plan, providers)
        findings = await self.read(user, results, plan, searcher, fresh, providers)
        kept = await self.cache.keep(user, findings, plan.freshness, target)
        return WebOutcome.sent(kept, plan.query, plan.lane, self.contacted(providers))

    @staticmethod
    def contacted(providers: list[str]) -> tuple[str, ...]:
        """The providers this call actually reached, named once each in the order tried."""
        return tuple(dict.fromkeys(providers))

    def nothing_found(
        self, reason: Refusal, plan: SanctionedPlan, providers: list[str]
    ) -> WebOutcome:
        """The outcome when no usable result came back, told apart by whether anyone was
        asked."""
        if reached := self.contacted(providers):
            return WebOutcome.fruitless(reason, plan.query, plan.lane, reached)
        return WebOutcome.refused(reason)

    async def search(
        self, user: User, plan: SanctionedPlan, providers: list[str]
    ) -> tuple[tuple[WebResult, ...], WebSearcher | None]:
        """Walk this lane's chain until one provider answers, charging each attempt.

        The allowance is charged before the request is made rather than after it succeeds,
        because an attempt that fails still costs the provider a call, and a caller who has
        run out must be stopped before the socket opens rather than after.
        """
        for attempt, searcher in enumerate(self.searchers(plan.lane)):
            await monthly_quota.consume(user.id, UsageEvent.Operation.web_search, searcher.spend)
            providers.append(searcher.name)
            started = perf_counter()
            try:
                results = await searcher.search(plan.query, self.config.web_search_results)
            except ProviderUnavailable:
                await self.account(
                    user, UsageEvent.Operation.web_search, searcher, attempt, started
                )
                continue
            await self.account(user, UsageEvent.Operation.web_search, searcher, attempt, started)
            if results:
                return results, searcher
        return (), None

    async def read(
        self,
        user: User,
        results: tuple[WebResult, ...],
        plan: SanctionedPlan,
        searcher: WebSearcher,
        fresh: bool,
        providers: list[str],
    ) -> tuple[WebFinding, ...]:
        """Turn the top hits into readable findings, falling back to the hit's own preview.

        A hit whose page nobody could read still carries the title and snippet the search
        provider returned, so a fetch chain that is down degrades the answer instead of
        emptying it. Such a finding is never storable, because that text belongs to the
        search vendor rather than to the page, which is exactly the licence a provider that
        declares itself unpersistable is asserting.
        """
        found: list[WebFinding] = []
        for result in results[: self.config.web_search_pages]:
            page = await self.fetch(user, result.url, plan.freshness, fresh, providers)
            found.append(
                self.preview_finding(result, searcher) if page is None else self.page_finding(page)
            )
        return tuple(found)

    def preview_finding(self, result: WebResult, searcher: WebSearcher) -> WebFinding:
        """One hit rendered from the search row alone, kept out of the cache by its licence."""
        return WebFinding(
            url=result.url,
            text=result.preview[: self.config.web_search_page_max_chars],
            provider=searcher.name,
            retrieved_at=datetime.now(UTC),
            title=result.title,
            persistable=False,
        )

    def page_finding(self, page: WebPage) -> WebFinding:
        """One hit rendered from the page a reader actually retrieved."""
        return WebFinding(
            url=page.url,
            text=page.trimmed(self.config.web_search_page_max_chars),
            provider=page.provider,
            retrieved_at=page.retrieved_at,
            title=page.title,
            persistable=page.persistable,
        )

    async def fetch(
        self,
        user: User,
        url: AnyHttpUrl,
        freshness: Freshness,
        force: bool,
        providers: list[str],
    ) -> WebPage | None:
        """Walk the fetch chain for one link, or return nothing when none of it answered."""
        for fetcher in self.fetchers(freshness):
            try:
                await monthly_quota.consume(user.id, UsageEvent.Operation.web_fetch)
            except QuotaExceededError:
                logger.warning("stopped fetching pages, the monthly web allowance is spent")
                return None
            providers.append(fetcher.name)
            started = perf_counter()
            try:
                page = await fetcher.fetch(url, force)
            except ProviderUnavailable:
                await self.account(
                    user, UsageEvent.Operation.web_fetch, fetcher, self.ordinal(providers), started
                )
                continue
            await self.account(
                user, UsageEvent.Operation.web_fetch, fetcher, self.ordinal(providers), started
            )
            return page
        return None

    @staticmethod
    def ordinal(providers: list[str]) -> int:
        """How many provider calls this egress has made, so no two ledger rows collide."""
        return len(providers) - 1

    async def account(
        self,
        user: User,
        operation: UsageEvent.Operation,
        provider: WebSearcher | WebFetcher,
        ordinal: int,
        started: float,
    ) -> None:
        """Record one external provider call as its own durable ledger row.

        Credits ride in `items` and the true wire sizes in the byte columns, read off the
        request and response the provider actually exchanged, so a month's bill can be
        reconciled against the ledger without asking the provider. The ordinal keeps two
        calls to one provider inside one request from folding into a single row.
        """
        await self.recorder.record(
            capture_step(
                operation,
                user.id,
                (user.id,),
                f"{operation.value}:{provider.name}",
                ordinal=ordinal,
                items=provider.spend,
                request_bytes=provider.traffic.request_bytes,
                response_bytes=provider.traffic.response_bytes,
                duration_ms=max(0.0, (perf_counter() - started) * 1000),
            )
        )

    def searchers(self, lane: SearchLane) -> tuple[WebSearcher, ...]:
        """This lane's configured, buildable providers in preference order.

        A name no provider claims is a configuration mistake rather than a reason to stop,
        so it is logged and skipped and the rest of the chain still runs.
        """
        built: list[WebSearcher | None] = []
        for name in lane_providers(self.config, kind=lane):
            try:
                built.append(WebSearcher.find(name).from_settings(self.config))
            except KeyError:
                logger.warning("ignored unknown web search provider {}", name)
        return tuple(provider for provider in built if provider is not None)

    def fetchers(self, freshness: Freshness) -> tuple[WebFetcher, ...]:
        """The configured page readers in preference order, unbuildable ones dropped."""
        available: dict[str, WebFetcher | None] = {
            "firecrawl-reader": FirecrawlReader.from_settings(self.config, freshness),
            "docling-reader": DoclingReader(reader=self.reader, converter=self.converter),
        }
        return tuple(
            reader
            for name in self.config.web_search_fetch_providers
            if (reader := available.get(name)) is not None
        )

    async def probe(
        self, user: User, query: str, evidence: FindEvidence, fresh: bool, execute: bool
    ) -> RouterProbe:
        """Run the router and the sanitizer, calling providers only when asked to.

        The operator tool. It settles the one question a deployment needs answered before it
        turns egress on, which is what exactly would leave the machine for this question.
        Providers stay untouched unless `execute` says otherwise, so a probe is free and
        safe to run against real questions on a deployment that has egress switched off.
        """
        roster = await self.roster(user)
        signals = self.router.signals(
            query, evidence.candidates, evidence.scores, evidence.mentions, roster
        )
        routed = await self.router.plan(query, signals, skip_sufficiency=fresh, skip_roster=False)
        if isinstance(routed, Refusal):
            return RouterProbe(query=query, signals=signals, refusal=routed)
        sanitizer = QuerySanitizer.build(self.config, self.gate, roster)
        refused = await sanitizer.refuses(routed.query)
        return RouterProbe(
            query=query,
            signals=signals,
            plan=routed,
            sanitizer=refused,
            findings=(await self.rehearse(user, routed) if execute and refused is None else ()),
        )

    async def rehearse(self, user: User, plan: SanctionedPlan) -> tuple[str, ...]:
        """Run one real search through the ordinary egress path so an operator can read it.

        A rehearsal that skipped the switches, the allowance and the ledger would be a
        second, quieter way out of the machine, which is exactly the thing this feature must
        not have. It goes through `search`, so it refuses when the deployment refuses,
        charges what a search charges, and leaves the same usage rows behind.
        """
        if self.permitted(user) is not None:
            logger.warning("refused to rehearse, this deployment or account may not reach the web")
            return ()
        try:
            results, _ = await self.search(user, plan, [])
        except QuotaExceededError:
            logger.warning("refused to rehearse, the monthly web allowance is spent")
            return ()
        return tuple(str(result.url) for result in results)
