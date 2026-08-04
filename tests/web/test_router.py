from datetime import UTC, datetime, timedelta

import dbutil
import httpx
import pytest
from doubles import FakeLLM
from id_factory import uuid7
from pydantic_ai.exceptions import UnexpectedModelBehavior
from web_doubles import ScriptedGate, as_gate

from aizk.config import Settings, settings
from aizk.integrations.web import Freshness, SearchLane
from aizk.retrieval import Candidate, Lane
from aizk.web import (
    MemorySignals,
    QuerySanitizer,
    Refusal,
    SanctionedPlan,
    WebQueryPlan,
    WebRouter,
)
from aizk.web.router import RouterProbe


def tuned(**overrides: object) -> Settings:
    """Settings with the egress parameters a test wants to move."""
    return settings.model_copy(update=overrides)


def router(llm: FakeLLM | None = None, **overrides: object) -> WebRouter:
    """A router over a faked planner, so no model is ever dialed."""
    return WebRouter(tuned(**overrides), (llm or FakeLLM()).llm)


def memory(
    line: str = "a stored line",
    direct: bool = False,
    web_cache: bool = False,
    document_expires_at: datetime | None = None,
) -> Candidate:
    """One packed memory candidate with a stable evidence identity."""
    return Candidate(
        lane=Lane.Kind.SOURCES,
        line=line,
        evidence_id=uuid7(),
        direct=direct,
        web_cache=web_cache,
        document_expires_at=document_expires_at,
    )


def plan(
    needs_web: bool = True,
    search_query: str | None = "how does a public thing work",
    lane: SearchLane = SearchLane.keyword,
    freshness: Freshness = Freshness.dated,
) -> WebQueryPlan:
    """A planner answer that asks for the web unless a test says otherwise."""
    return WebQueryPlan(
        needs_web=needs_web,
        reason="memory holds nothing public",
        search_query=search_query,
        lane=lane,
        freshness=freshness,
    )


def test_signals_count_only_candidates_that_clear_the_rerank_floor() -> None:
    strong, weak, named = memory("strong"), memory("weak"), memory("named", direct=True)
    scores = {strong.evidence_id: 0.9, weak.evidence_id: 0.1, named.evidence_id: 0.2}

    signals = router().signals("a question", (strong, weak, named), scores, (), frozenset())

    assert (signals.strong, signals.direct) == (1, 1)
    assert signals.sufficient(2)
    assert not signals.sufficient(3)
    assert "strong" in signals.summary


def test_signals_report_no_memory_at_all_rather_than_an_empty_prompt() -> None:
    assert router().signals("a question", (), {}, (), frozenset()).summary == "(nothing)"


def test_a_stale_cached_page_stops_counting_toward_sufficiency() -> None:
    now = datetime.now(UTC)
    fresh_page = memory("fresh cache", web_cache=True, document_expires_at=now + timedelta(days=1))
    stale_page = memory(
        "stale cache", web_cache=True, document_expires_at=now - timedelta(minutes=1)
    )
    # a page with no expiry at all is a page nothing retired, so it still answers
    endless = memory("endless cache", web_cache=True)
    scores = dict.fromkeys(
        (fresh_page.evidence_id, stale_page.evidence_id, endless.evidence_id), 0.9
    )

    signals = router().signals("q", (fresh_page, stale_page, endless), scores, (), frozenset())

    assert signals.strong == 2
    assert "stale cache" not in signals.summary


def test_a_query_naming_a_stored_entity_registers_as_a_roster_hit() -> None:
    hit = router().signals("q", (), {}, ("Atlas",), frozenset({"atlas"}))
    miss = router().signals("q", (), {}, ("Atlas",), frozenset({"borealis"}))

    assert hit.roster_hit and not miss.roster_hit


def test_a_world_marker_is_read_off_the_question_itself() -> None:
    outward = router().signals("what is the latest release", (), {}, (), frozenset())
    inward = router().signals("what did we decide", (), {}, (), frozenset())

    assert outward.world_marker and not inward.world_marker


def test_memory_that_answers_the_question_ends_the_call_before_any_model_runs() -> None:
    fake = FakeLLM()
    signals = MemorySignals(strong=3, answering=3)

    verdict = dbutil.run(
        router(fake).plan("q", signals, skip_sufficiency=False, skip_roster=False)
    )

    assert verdict is Refusal.memory_answered
    assert fake.completions.calls == []


def test_a_private_subject_without_a_world_marker_never_reaches_the_planner() -> None:
    fake = FakeLLM()
    signals = MemorySignals(roster_hit=True, world_marker=False)

    verdict = dbutil.run(
        router(fake).plan("q", signals, skip_sufficiency=False, skip_roster=False)
    )

    assert verdict is Refusal.private_subject
    assert fake.completions.calls == []


def test_a_private_subject_with_a_world_marker_is_answered_as_both_halves() -> None:
    fake = FakeLLM()
    fake.register(WebQueryPlan, plan())
    signals = MemorySignals(roster_hit=True, world_marker=True)

    verdict = dbutil.run(
        router(fake).plan("q", signals, skip_sufficiency=False, skip_roster=False)
    )

    assert isinstance(verdict, SanctionedPlan)
    assert verdict.query == "how does a public thing work"


@pytest.mark.parametrize(
    "failure",
    [
        UnexpectedModelBehavior("garbled"),
        httpx.ConnectError("no route"),
        TimeoutError("too slow"),
        OSError("socket gone"),
        ValueError("unreadable"),
    ],
)
def test_a_planner_that_did_not_answer_authorises_nothing(failure: Exception) -> None:
    fake = FakeLLM()
    fake.completions.error = failure

    verdict = dbutil.run(
        router(fake).plan("q", MemorySignals(), skip_sufficiency=False, skip_roster=False)
    )

    assert verdict is Refusal.planner_unavailable


def test_a_planner_saying_no_ends_the_call_in_memory() -> None:
    fake = FakeLLM()
    fake.register(WebQueryPlan, plan(needs_web=False))

    verdict = dbutil.run(
        router(fake).plan("q", MemorySignals(), skip_sufficiency=False, skip_roster=False)
    )

    assert verdict is Refusal.planner_declined


@pytest.mark.parametrize(
    "answer",
    [plan(search_query=None), plan(lane=SearchLane.none)],
    ids=["no rewrite", "no lane"],
)
def test_a_planner_that_cannot_write_a_public_question_forbids_egress(
    answer: WebQueryPlan,
) -> None:
    fake = FakeLLM()
    fake.register(WebQueryPlan, answer)

    verdict = dbutil.run(
        router(fake).plan("q", MemorySignals(), skip_sufficiency=False, skip_roster=False)
    )

    assert verdict is Refusal.sanitizer_refused


def test_skipping_sufficiency_still_asks_the_planner_and_ignores_only_its_verdict() -> None:
    fake = FakeLLM()
    fake.register(WebQueryPlan, plan(needs_web=False))

    verdict = dbutil.run(
        router(fake).plan(
            "q",
            MemorySignals(strong=99, answering=99, roster_hit=True),
            skip_sufficiency=True,
            skip_roster=True,
        )
    )

    assert isinstance(verdict, SanctionedPlan)
    assert verdict.freshness is Freshness.dated
    assert fake.completions.calls != []


def test_the_planner_prompt_carries_the_question_and_the_memory_it_must_judge() -> None:
    fake = FakeLLM()
    fake.register(WebQueryPlan, plan())

    dbutil.run(
        router(fake).plan(
            "what changed",
            MemorySignals(summary="- a stored line"),
            skip_sufficiency=False,
            skip_roster=False,
        )
    )

    (call,) = fake.completions.calls
    user = call.messages[1]["content"]
    assert "what changed" in user
    assert "- a stored line" in user
    assert "no personal name" in call.messages[0]["content"]


def test_a_probe_renders_the_one_line_an_operator_reads_first() -> None:
    approved = RouterProbe(
        query="what changed",
        signals=MemorySignals(strong=1, world_marker=True),
        plan=SanctionedPlan(
            query="how does a public thing work",
            lane=SearchLane.keyword,
            freshness=Freshness.stable,
            reason="memory holds nothing public",
        ),
        findings=("https://example.test/page",),
    )

    rendered = approved.render()

    assert approved.egress_query == "how does a public thing work"
    assert "egress     how does a public thing work" in rendered
    assert "freshness  stable" in rendered
    assert "found      https://example.test/page" in rendered


@pytest.mark.parametrize(
    "probe",
    [
        RouterProbe(query="q", signals=MemorySignals(), refusal=Refusal.private_subject),
        RouterProbe(
            query="q",
            signals=MemorySignals(),
            plan=SanctionedPlan(
                query="leaked name",
                lane=SearchLane.keyword,
                freshness=Freshness.stable,
                reason="r",
            ),
            sanitizer=Refusal.sanitizer_refused,
        ),
    ],
    ids=["router refused", "sanitizer refused"],
)
def test_a_refused_probe_never_prints_a_query_as_though_it_would_be_sent(
    probe: RouterProbe,
) -> None:
    assert probe.egress_query is None
    assert "egress     refused" in probe.render()


def test_every_refusal_reason_reads_as_a_sentence_a_person_can_act_on() -> None:
    assert all(reason.because for reason in Refusal)


def test_the_sanitizer_is_built_from_the_deployment_detector_policy() -> None:
    gate = ScriptedGate()

    sanitizer = QuerySanitizer.build(tuned(), as_gate(gate), frozenset({"atlas", "ai"}))

    assert sanitizer.threshold == settings.web_search_detector_threshold
    assert sanitizer.labels == settings.web_search_detector_labels
    assert sanitizer.guarded == frozenset({"atlas"})
