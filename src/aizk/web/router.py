from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime

import httpx
from loguru import logger
from patos import FrozenModel
from pydantic import UUID5, UUID7
from pydantic_ai.exceptions import AgentRunError

from ..config import Settings
from ..integrations.web import SearchLane
from ..retrieval import Candidate
from ..serving.extract import LLM
from .models import MemorySignals, Refusal, SanctionedPlan, WebQueryPlan

_planner_template = "Question\n{query}\n\nMemory evidence already gathered\n{summary}"


class WebRouter:
    """Decide whether one question may reach the public web, and as what.

    The order of the decision is the whole design. Memory runs first and for free, and its
    own signals settle most questions before anything else is consulted. Only what memory
    cannot answer reaches the local planner, and only what the planner can restate as a
    stranger's question is offered to the sanitizer. The public web is the last resort of a
    question that has already failed to be private.

        memory candidates and their rerank scores
                    |
        sufficient? -> yes, stop, nothing leaves
                    |
        roster hit and no world marker? -> yes, stop, nothing leaves
                    |
        one local planner turn, classify and rewrite together
                    |
        needs_web and a non-null rewrite? -> no, stop, nothing leaves
                    |
        the sanitizer's independent verdict on the rewrite

    The roster branch is the one that matters most and costs least. A question naming
    something the caller already stores, with no word pointing at the public world, is a
    question about their own life, and it ends here without a model call and without a byte
    of egress. When such a question does carry a world marker it goes on to the planner, and
    if the planner asks for the web the call runs as both halves rather than as the web
    alone, because memory evidence always renders first and private context is never sent
    out on its own.
    """

    def __init__(self, config: Settings, llm: LLM) -> None:
        self.config = config
        self.llm = llm

    def signals(
        self,
        query: str,
        candidates: Sequence[Candidate],
        scores: Mapping[UUID5 | UUID7 | None, float],
        mentions: Iterable[str],
        roster: frozenset[str],
    ) -> MemorySignals:
        """Read the free signals the memory half of this call already produced.

        A cached web page counts toward sufficiency only while it is still fresh, judged by
        the expiry its own freshness bucket earned rather than by one horizon for all three.
        Expiry in the store is the real enforcement, since an expired document leaves
        retrieval entirely, and this second cut refuses to let a page that survived one
        sweep speak for a question it can no longer answer.
        """
        floor = self.config.web_search_rerank_floor
        now = datetime.now(UTC)
        usable = [item for item in candidates if self.current(item, now)]
        lowered = query.lower()
        strong = [item for item in usable if scores.get(item.evidence_id, 0.0) >= floor]
        return MemorySignals(
            strong=len(strong),
            direct=sum(item.direct for item in usable),
            answering=len({id(item) for item in strong} | {id(i) for i in usable if i.direct}),
            roster_hit=bool(roster & {mention.lower() for mention in mentions}),
            world_marker=any(marker in lowered for marker in self.config.web_search_world_markers),
            summary=self.summary(usable),
        )

    @staticmethod
    def current(candidate: Candidate, moment: datetime) -> bool:
        """Whether this candidate may speak for the question at all."""
        return not candidate.web_cache or candidate.current_at(moment)

    def summary(self, candidates: Sequence[Candidate]) -> str:
        """The memory excerpt the local planner reads, bounded so the turn stays one call."""
        joined = "\n".join(f"- {item.line}" for item in candidates)
        return joined[: self.config.web_search_page_max_chars] or "(nothing)"

    async def plan(
        self,
        query: str,
        signals: MemorySignals,
        skip_sufficiency: bool,
        skip_roster: bool,
    ) -> SanctionedPlan | Refusal:
        """The public question this call may ask, or the reason it may ask none.

        The two levers are separate because they lift different guarantees. Skipping
        sufficiency only overrules memory's own judgement that it had enough, which is what
        a caller asking for a fresh read is entitled to overrule. Skipping the roster lifts
        the stop that keeps a question about the caller's own world from being planned at
        all, which is a privacy decision and belongs to `force` alone.
        """
        if not skip_sufficiency and signals.sufficient(
            self.config.web_search_sufficient_candidates
        ):
            return Refusal.memory_answered
        if not skip_roster and signals.roster_hit and not signals.world_marker:
            return Refusal.private_subject
        plan = await self.ask(query, signals)
        if plan is None:
            return Refusal.planner_unavailable
        if not (plan.needs_web or skip_sufficiency):
            return Refusal.planner_declined
        if plan.search_query is None or plan.lane is SearchLane.none:
            return Refusal.sanitizer_refused
        return SanctionedPlan(
            query=plan.search_query,
            lane=plan.lane,
            freshness=plan.freshness,
            reason=plan.reason,
        )

    async def ask(self, query: str, signals: MemorySignals) -> WebQueryPlan | None:
        """One structured turn through the local model lane, or nothing when it fails.

        Every way this can go wrong is the same answer. A model that timed out, refused, or
        returned something unreadable has not authorised anything, and an unauthorised call
        does not go out.
        """
        try:
            return await self.llm.generate(
                self.config.web_search_planner_system,
                _planner_template.format(query=query, summary=signals.summary),
                WebQueryPlan,
            )
        except (AgentRunError, httpx.HTTPError, OSError, TimeoutError, ValueError) as failed:
            logger.warning("the web planner did not answer, so nothing was sent: {}", failed)
            return None


class RouterProbe(FrozenModel):
    """One operator-visible account of what the router and sanitizer decided.

    `aizk admin web probe` prints this before a deployment ever turns egress on, so the
    exact query that would leave the machine can be read by a person first.
    """

    query: str
    signals: MemorySignals
    plan: SanctionedPlan | None = None
    refusal: Refusal | None = None
    sanitizer: Refusal | None = None
    findings: tuple[str, ...] = ()

    @property
    def egress_query(self) -> str | None:
        """The exact text that would be sent, or nothing when the call refuses."""
        if self.plan is None or self.sanitizer is not None:
            return None
        return self.plan.query

    def render(self) -> str:
        """Render the probe for a terminal, leading with the one line that matters."""
        outcome = self.egress_query
        lines = [
            f"query      {self.query}",
            f"egress     {outcome if outcome is not None else 'refused'}",
            (
                f"signals    strong {self.signals.strong}  direct {self.signals.direct}  "
                f"roster {self.signals.roster_hit}  world marker {self.signals.world_marker}"
            ),
        ]
        if self.refusal is not None:
            lines.append(f"refusal    {self.refusal.value}, {self.refusal.because}")
        if self.plan is not None:
            lines.extend(
                (
                    f"lane       {self.plan.lane.value}",
                    f"freshness  {self.plan.freshness.value}",
                    f"reason     {self.plan.reason}",
                )
            )
        if self.sanitizer is not None:
            lines.append(f"sanitizer  refused, {self.sanitizer.because}")
        lines.extend(f"found      {url}" for url in self.findings)
        return "\n".join(lines)
