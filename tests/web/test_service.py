import asyncio
from typing import cast

import dbutil
import pytest
from doubles import FakeLLM
from id_factory import uuid5
from pydantic import SecretStr
from web_doubles import (
    InertPageConverter,
    InertPageSource,
    RecordingRecorder,
    ScriptedFetcher,
    ScriptedGate,
    ScriptedScanner,
    ScriptedSearcher,
    as_gate,
    as_recorder,
    as_scanner,
    hit,
    page,
)

from aizk.config import Settings, settings
from aizk.exceptions import QuotaExceededError, ScopeNotFoundError
from aizk.integrations.web import (
    DoclingReader,
    FirecrawlReader,
    Freshness,
    SearchLane,
    WebFetcher,
    WebResult,
    WebSearcher,
)
from aizk.retrieval import RecallEvidence
from aizk.store import Usage
from aizk.store.identity import OrganizationStanding, User
from aizk.web import (
    Refusal,
    SanctionedPlan,
    WebMode,
    WebOutcome,
    WebQueryPlan,
    WebSearch,
)

pytestmark = pytest.mark.usefixtures("migrated_db")

_WEB_ORG = settings.web_search_organization


def enabled(**overrides: object) -> Settings:
    """Settings with the deployment switch on and one searchable lane configured."""
    return settings.model_copy(update={"web_search_enabled": True, **overrides})


def member(name: str = _WEB_ORG) -> User:
    """A caller who belongs to the organization that grants egress."""
    owner, org = uuid5(), uuid5()
    return User.authorized(
        owner,
        read=(owner, org),
        write=(owner, org),
        organizations=(OrganizationStanding(id=org, name=name),),
    )


class Service(WebSearch):
    """A web service whose provider chains are scripted rather than configured."""

    def __init__(
        self,
        config: Settings,
        llm: FakeLLM,
        searchers: tuple[WebSearcher, ...] = (),
        fetchers: tuple[WebFetcher, ...] = (),
        gate: ScriptedGate | None = None,
        scanner: ScriptedScanner | None = None,
        recorder: RecordingRecorder | None = None,
    ) -> None:
        super().__init__(
            config,
            llm.llm,
            as_gate(gate or ScriptedGate()),
            InertPageSource(),
            InertPageConverter(),
            as_scanner(scanner or ScriptedScanner()),
            as_recorder(recorder or RecordingRecorder()),
        )
        self.scripted_searchers = searchers
        self.scripted_fetchers = fetchers

    def searchers(self, lane: SearchLane) -> tuple[WebSearcher, ...]:
        del lane
        return self.scripted_searchers

    def fetchers(self, freshness: Freshness) -> tuple[WebFetcher, ...]:
        del freshness
        return self.scripted_fetchers


def planner(needs_web: bool = True) -> FakeLLM:
    """A faked planner that asks for the web with a clean public rewrite."""
    fake = FakeLLM()
    fake.register(
        WebQueryPlan,
        WebQueryPlan(
            needs_web=needs_web,
            reason="memory holds nothing public",
            search_query="how does a public thing work",
            lane=SearchLane.keyword,
            freshness=Freshness.dated,
        ),
    )
    return fake


def run(service: WebSearch, user: User, mode: WebMode = WebMode.auto, **kwargs: object):
    """Run one call's web half over an empty memory result."""
    return dbutil.run(
        service.run(
            user,
            cast("str", kwargs.get("query", "what changed upstream")),
            RecallEvidence(),
            mode,
            cast("bool", kwargs.get("fresh", False)),
            cast("list[str] | None", kwargs.get("scopes")),
        )
    )


def test_web_off_keeps_the_call_entirely_local_and_says_so() -> None:
    outcome = run(Service(enabled(), planner()), member(), WebMode.off)

    assert outcome.findings == ()
    assert "web access was off for this call" in outcome.receipt


@pytest.mark.parametrize(
    ("config", "caller", "reason"),
    [
        (settings, member(), "the deployment switch is off"),
        (enabled(), User.private(settings.anonymous_user_id), "the caller is anonymous"),
        (enabled(), member("Somewhere Else"), "the caller is not in the web organization"),
    ],
)
def test_every_kill_switch_refuses_before_anything_is_planned(
    config: Settings, caller: User, reason: str
) -> None:
    fake = planner()

    outcome = run(Service(config, fake), caller)

    assert outcome.findings == ()
    assert "may not reach the web" in outcome.receipt, reason
    assert fake.completions.calls == []


def test_a_permitted_caller_passes_the_switches() -> None:
    assert Service(enabled(), planner()).permitted(member()) is None


def test_the_router_refusal_becomes_the_receipt_without_any_provider_call() -> None:
    searcher = ScriptedSearcher()
    fake = FakeLLM()
    fake.register(WebQueryPlan, WebQueryPlan(needs_web=False, reason="memory answered it"))

    outcome = run(Service(enabled(), fake, searchers=(searcher,)), member())

    assert outcome.findings == ()
    assert "the planner judged that the public web could not help" in outcome.receipt
    assert "went to the configured extraction endpoint" in outcome.receipt
    assert searcher.calls == []


def test_a_rewrite_the_sanitizer_refuses_never_reaches_a_provider() -> None:
    searcher = ScriptedSearcher()
    gate = ScriptedGate(mentions=["a private name"])

    outcome = run(Service(enabled(), planner(), searchers=(searcher,), gate=gate), member())

    assert outcome.findings == ()
    assert "cannot be asked publicly" in outcome.receipt
    assert searcher.calls == []


def test_a_sanctioned_call_sends_only_the_rewrite_and_names_it_in_the_receipt() -> None:
    searcher = ScriptedSearcher(results=(hit(),))
    fetcher = ScriptedFetcher(page())
    recorder = RecordingRecorder()
    service = Service(
        enabled(),
        planner(),
        searchers=(searcher,),
        fetchers=(fetcher,),
        recorder=recorder,
    )

    outcome = run(service, member(), query="what does the private project need")

    assert searcher.calls == ["how does a public thing work"]
    assert "what does the private project need" not in outcome.receipt
    assert "`how does a public thing work`" in outcome.receipt
    assert "reached the keyword lane" in outcome.receipt
    assert "under zero data retention" in outcome.receipt
    assert [finding.text for finding in outcome.findings] == ["the public page"]
    assert {capture.operation for capture in recorder.captures} == {
        Usage.Event.Operation.web_search,
        Usage.Event.Operation.web_fetch,
    }


def test_one_ledger_row_is_written_per_external_provider_call() -> None:
    refusing = ScriptedSearcher()
    answering = ScriptedSearcher(results=(hit(),))
    recorder = RecordingRecorder()
    service = Service(
        enabled(),
        planner(),
        searchers=(refusing, answering),
        fetchers=(ScriptedFetcher(page()),),
        recorder=recorder,
    )

    run(service, member())

    searches = [
        capture
        for capture in recorder.captures
        if capture.operation is Usage.Event.Operation.web_search
    ]
    fetches = [
        capture
        for capture in recorder.captures
        if capture.operation is Usage.Event.Operation.web_fetch
    ]
    assert len(searches) == 2  # the refusal cost a call too, so it is accounted for
    assert len(fetches) == 1
    assert len({capture.capture_key for capture in recorder.captures}) == 3
    assert searches[-1].items == 1
    assert searches[-1].request_bytes == len(b"how does a public thing work")
    assert fetches[0].response_bytes == len("the public page")


def test_a_whole_chain_that_refuses_degrades_to_memory_with_the_reason() -> None:
    searcher = ScriptedSearcher(fails=True)

    outcome = run(Service(enabled(), planner(), searchers=(searcher,)), member())

    assert outcome.findings == ()
    assert "no search provider answered" in outcome.receipt


def test_a_lane_with_no_configured_provider_at_all_degrades_the_same_way() -> None:
    outcome = run(Service(enabled(), planner()), member())

    assert outcome.findings == ()
    assert "no search provider answered" in outcome.receipt


def test_a_page_no_reader_could_open_still_answers_from_the_search_row() -> None:
    searcher = ScriptedSearcher(results=(hit(snippet="what the vendor showed"),))
    fetcher = ScriptedFetcher(None)

    outcome = run(
        Service(enabled(), planner(), searchers=(searcher,), fetchers=(fetcher,)), member()
    )

    (finding,) = outcome.findings
    assert "what the vendor showed" in finding.text
    assert finding.persistable is False


def test_the_fetch_chain_falls_through_to_the_next_reader() -> None:
    searcher = ScriptedSearcher(results=(hit(),))
    first, second = ScriptedFetcher(None, name="first"), ScriptedFetcher(page(), name="second")

    outcome = run(
        Service(enabled(), planner(), searchers=(searcher,), fetchers=(first, second)), member()
    )

    assert first.calls and second.calls
    assert outcome.findings[0].provider == "scripted-reader"


def test_a_fresh_call_skips_sufficiency_and_asks_every_reader_for_a_live_read() -> None:
    searcher = ScriptedSearcher(results=(hit(),))
    fetcher = ScriptedFetcher(page())
    fake = planner(needs_web=False)

    run(
        Service(enabled(), fake, searchers=(searcher,), fetchers=(fetcher,)),
        member(),
        fresh=True,
    )

    assert fetcher.calls == [("https://example.test/page", True)]


def test_forcing_the_web_skips_sufficiency_but_never_the_sanitizer() -> None:
    searcher = ScriptedSearcher()
    gate = ScriptedGate(mentions=["a private name"])

    outcome = run(
        Service(enabled(), planner(needs_web=False), searchers=(searcher,), gate=gate),
        member(),
        WebMode.force,
    )

    assert "cannot be asked publicly" in outcome.receipt
    assert searcher.calls == []


def test_an_exhausted_allowance_stops_the_call_before_the_socket_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = ScriptedSearcher(results=(hit(),))

    async def spent(user_id: object, operation: object, units: int = 1) -> None:
        del user_id, operation, units
        raise QuotaExceededError("monthly web limit reached")

    monkeypatch.setattr("aizk.web.service.monthly_quota.consume", spent)

    outcome = run(
        Service(enabled(), planner(), searchers=(searcher,), fetchers=(ScriptedFetcher(page()),)),
        member(),
    )

    assert outcome.findings == ()
    assert "monthly web allowance is spent" in outcome.receipt
    assert searcher.calls == []


def test_an_allowance_spent_between_pages_stops_fetching_rather_than_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = ScriptedSearcher(results=(hit(),))
    fetcher = ScriptedFetcher(page())
    calls: list[object] = []

    async def spent_on_fetch(user_id: object, operation: object, units: int = 1) -> None:
        del user_id, units
        calls.append(operation)
        if operation is Usage.Event.Operation.web_fetch:
            raise QuotaExceededError("monthly web limit reached")

    monkeypatch.setattr("aizk.web.service.monthly_quota.consume", spent_on_fetch)

    outcome = run(
        Service(enabled(), planner(), searchers=(searcher,), fetchers=(fetcher,)), member()
    )

    assert fetcher.calls == []
    assert outcome.findings[0].persistable is False  # the search row, not a page
    assert "reached the keyword lane" in outcome.receipt


def test_an_unwritable_cache_destination_is_refused_before_anything_is_planned() -> None:
    with pytest.raises(ScopeNotFoundError):
        run(Service(enabled(), planner()), member(), scopes=["Nowhere"])


def test_the_configured_chain_skips_a_provider_name_nothing_claims() -> None:
    service = WebSearch(
        enabled(web_search_keyword_providers=("nonesuch", "firecrawl")),
        planner().llm,
        as_gate(ScriptedGate()),
        InertPageSource(),
        InertPageConverter(),
        as_scanner(ScriptedScanner()),
    )

    assert service.searchers(SearchLane.keyword) == ()


def test_the_fetch_chain_is_built_in_the_configured_order() -> None:
    service = WebSearch(
        enabled(web_search_fetch_providers=("docling-reader", "nonesuch")),
        planner().llm,
        as_gate(ScriptedGate()),
        InertPageSource(),
        InertPageConverter(),
        as_scanner(ScriptedScanner()),
    )

    built = service.fetchers(Freshness.stable)

    assert [reader.name for reader in built] == ["docling-reader"]


def test_a_probe_reports_the_plan_without_calling_a_single_provider() -> None:
    caller = member()
    searcher = ScriptedSearcher(results=(hit(),))
    service = Service(enabled(), planner(), searchers=(searcher,))

    probe = dbutil.run(service.probe(caller, "what changed", RecallEvidence(), False, False))

    assert probe.egress_query == "how does a public thing work"
    assert probe.findings == ()
    assert searcher.calls == []


def test_a_probe_asked_to_execute_runs_one_real_search_for_an_operator_to_read() -> None:
    caller = member()
    refusing = ScriptedSearcher(fails=True)
    answering = ScriptedSearcher(results=(hit(),))
    service = Service(enabled(), planner(), searchers=(refusing, answering))

    probe = dbutil.run(service.probe(caller, "what changed", RecallEvidence(), False, True))

    assert probe.findings == ("https://example.test/page",)
    assert answering.calls == ["how does a public thing work"]


def test_a_probe_that_the_router_refused_reports_the_refusal_and_stops() -> None:
    fake = FakeLLM()
    fake.register(WebQueryPlan, WebQueryPlan(needs_web=False, reason="memory answered it"))
    caller = member()
    service = Service(enabled(), fake)

    probe = dbutil.run(service.probe(caller, "what changed", RecallEvidence(), False, False))

    assert probe.refusal is Refusal.planner_declined
    assert probe.plan is None


def test_a_probe_the_sanitizer_refused_never_rehearses_a_provider() -> None:
    caller = member()
    searcher = ScriptedSearcher(results=(hit(),))
    service = Service(
        enabled(), planner(), searchers=(searcher,), gate=ScriptedGate(mentions=["a name"])
    )

    probe = dbutil.run(service.probe(caller, "what changed", RecallEvidence(), False, True))

    assert probe.sanitizer is Refusal.sanitizer_refused
    assert probe.findings == ()
    assert searcher.calls == []


def test_a_rehearsal_with_no_answering_provider_reports_nothing_found() -> None:
    refusing = ScriptedSearcher(fails=True)
    service = Service(enabled(), planner(), searchers=(refusing,))
    plan = SanctionedPlan(
        query="how does a public thing work",
        lane=SearchLane.keyword,
        freshness=Freshness.stable,
        reason="r",
    )

    assert dbutil.run(service.rehearse(member(), plan)) == ()


def test_the_roster_a_sanitizer_checks_is_the_callers_own_visible_names() -> None:
    caller = member()
    service = Service(enabled(), planner())

    roster = dbutil.run(service.roster(caller))

    assert _WEB_ORG.lower() in roster


def test_the_real_readers_each_charge_one_credit_per_page() -> None:
    """A page is a page, whichever reader in the chain went and got it."""
    built = FirecrawlReader.from_settings(
        enabled(web_firecrawl_api_key=SecretStr("fc-key")), Freshness.stable
    )

    assert built is not None
    assert built.spend == 1
    assert DoclingReader(reader=InertPageSource(), converter=InertPageConverter()).spend == 1


def test_a_web_half_that_runs_out_of_time_answers_from_memory() -> None:
    """A slow provider costs a slow answer, never no answer."""

    class Slow(Service):
        async def decide(self, *args: object, **kwargs: object) -> WebOutcome:
            del args, kwargs
            await asyncio.sleep(1)
            raise AssertionError("the deadline must fire first")

    service = Slow(enabled(web_search_deadline=0.01), planner())

    outcome = run(service, member())

    assert outcome.findings == ()
    assert "no search provider answered" in outcome.receipt


def test_a_rehearsal_refuses_when_the_deployment_refuses() -> None:
    """The operator rehearsal is ordinary egress, so it stops where a call would stop."""
    searcher = ScriptedSearcher(results=(hit(),))
    service = Service(settings, planner(), searchers=(searcher,))
    plan = SanctionedPlan(
        query="how does a public thing work",
        lane=SearchLane.keyword,
        freshness=Freshness.stable,
        reason="r",
    )

    assert dbutil.run(service.rehearse(member(), plan)) == ()
    assert searcher.calls == []


def test_a_rehearsal_stops_when_the_allowance_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = ScriptedSearcher(results=(hit(),))
    service = Service(enabled(), planner(), searchers=(searcher,))

    async def spent(user_id: object, operation: object, units: int = 1) -> None:
        del user_id, operation, units
        raise QuotaExceededError("monthly web limit reached")

    monkeypatch.setattr("aizk.web.service.monthly_quota.consume", spent)
    plan = SanctionedPlan(
        query="how does a public thing work",
        lane=SearchLane.keyword,
        freshness=Freshness.stable,
        reason="r",
    )

    assert dbutil.run(service.rehearse(member(), plan)) == ()
    assert searcher.calls == []


def test_an_allowance_spent_mid_chain_still_names_the_providers_it_reached() -> None:
    """Reaching a provider and running out afterwards is not a call that stayed home."""
    spent: list[int] = []

    class Metered(Service):
        """A service whose allowance runs out after the first provider was already asked."""

        async def search(
            self, user: User, plan: SanctionedPlan, providers: list[str]
        ) -> tuple[tuple[WebResult, ...], WebSearcher | None]:
            del user, plan
            providers.append("scripted-searcher")
            spent.append(1)
            raise QuotaExceededError("monthly web limit reached")

    outcome = run(Metered(enabled(), planner()), member())

    assert "was sent to the keyword lane through `scripted-searcher`" in outcome.receipt
    assert "returned nothing usable" in outcome.receipt
    assert "monthly web allowance is spent" in outcome.receipt
