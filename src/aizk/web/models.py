from datetime import datetime
from enum import StrEnum, auto

from patos import FrozenModel
from pydantic import BaseModel, Field
from pydantic.networks import AnyHttpUrl

from ..integrations.web import Freshness, SearchLane


class WebMode(StrEnum):
    """What one `find` call is allowed to do about the public web."""

    # Spelled out rather than derived, because the `auto` member would shadow `auto()`.
    auto = "auto"
    off = "off"
    force = "force"


class WebQueryPlan(BaseModel):
    """The planner's single structured turn, classification and rewrite together.

    One model call answers both questions because they are one judgement. Deciding that a
    question needs the web means knowing which public question it becomes, and a planner
    that could not write that public question has, by saying so, decided the call must not
    go out. `search_query` is therefore the whole permission. A null forbids egress no
    matter what `needs_web` claims.
    """

    needs_web: bool = Field(description="whether the public web can add anything here")
    reason: str = Field(description="a short auditable justification")
    search_query: str | None = Field(
        default=None,
        description="the rewritten public question, or null when none can be written",
    )
    lane: SearchLane = Field(default=SearchLane.none, description="which external index to use")
    freshness: Freshness = Field(
        default=Freshness.stable, description="how fast the answer goes stale"
    )


class SanctionedPlan(FrozenModel):
    """A plan the router already approved, with the rewrite proven present.

    The distinction from `WebQueryPlan` is not cosmetic. A planner's answer may carry a
    null rewrite, which means no call may go out, so a nullable field is the right shape
    for what a model said. Everything downstream of the router deals only in calls that
    are going out, and giving that half a type whose query cannot be null is what keeps a
    null from having to be re-checked, or forgotten, at each later step.
    """

    query: str
    lane: SearchLane
    freshness: Freshness
    reason: str


class Refusal(StrEnum):
    """Why the search providers were not used, in the words the receipt prints."""

    memory_answered = auto()
    private_subject = auto()
    web_off = auto()
    not_permitted = auto()
    planner_unavailable = auto()
    planner_declined = auto()
    sanitizer_refused = auto()
    quota_exhausted = auto()
    provider_failure = auto()

    @property
    def because(self) -> str:
        """The clause the receipt reads after `because`."""
        return {
            Refusal.memory_answered: "your own memory already answered it",
            Refusal.private_subject: "the question is about your own notes, people or projects",
            Refusal.web_off: "web access was off for this call",
            Refusal.not_permitted: "this deployment or account may not reach the web",
            Refusal.planner_unavailable: "the planner could not decide, so nothing was searched",
            Refusal.planner_declined: "the planner judged that the public web could not help",
            Refusal.sanitizer_refused: (
                "the question cannot be asked publicly without naming something private"
            ),
            Refusal.quota_exhausted: "the monthly web allowance is spent",
            Refusal.provider_failure: "no search provider answered",
        }[self]

    @property
    def planned(self) -> bool:
        """Whether the caller's own question reached the extraction lane before this refusal.

        The four values below are all decided from memory alone, before a single byte is
        handed to any model, so a receipt printing one of them can honestly say the question
        stayed put. Every other value is reached after the planning turn, and pretending
        otherwise is exactly the falsehood a privacy receipt cannot afford.
        """
        return self not in {
            Refusal.memory_answered,
            Refusal.private_subject,
            Refusal.web_off,
            Refusal.not_permitted,
        }


class RosterName(FrozenModel):
    """One lowered entity name the caller can see, as the roster statement returns it."""

    name: str


class MemorySignals(FrozenModel):
    """What the free memory half of a `find` already knows about the question.

    Every signal here was computed by the retrieval that ran anyway, so consulting them
    costs nothing and a question memory can answer never reaches a model, let alone a
    third party.
    """

    strong: int = Field(default=0, description="candidates whose rerank score clears the floor")
    direct: int = Field(default=0, description="candidates whose complete title the query names")
    answering: int = Field(default=0, description="candidates counted once that clear either bar")
    roster_hit: bool = Field(
        default=False, description="the query names an entity the caller already stores"
    )
    world_marker: bool = Field(
        default=False, description="the query carries a word that points at the public world"
    )
    summary: str = Field(default="", description="the memory excerpt the planner is shown")

    def sufficient(self, floor: int) -> bool:
        """Whether memory answered the question on its own.

        One candidate is one answer. A well-ranked excerpt from a source the question names
        by name clears both bars, and adding the two counts would let it argue for itself
        twice and talk the call out of a search it needed.
        """
        return self.answering >= floor


class WebFinding(FrozenModel):
    """One public page this call actually read, ready to render and possibly to cache."""

    url: AnyHttpUrl
    text: str
    provider: str
    retrieved_at: datetime
    title: str | None = None
    persistable: bool = False


class WebOutcome(FrozenModel):
    """What one `find` call's web half did, and the receipt line stating it plainly.

    The receipt is the only place a caller can see where their words went, so it draws the
    line where the deployment actually draws it rather than where it would be flattering to.
    Planning is egress. The question and its memory excerpt go to the configured extraction
    endpoint, which in an ordinary production deployment is a hosted model pinned to zero
    data retention, and settings refuse to enable the web at all without that pin. Only the
    rewritten question ever reaches a search provider.
    """

    findings: tuple[WebFinding, ...] = ()
    receipt: str

    @staticmethod
    def planning() -> str:
        """The clause naming what the planning turn itself sent, and under what terms."""
        return (
            "Your question went to the configured extraction endpoint under zero data "
            "retention so the search could be planned"
        )

    @classmethod
    def refused(cls, reason: Refusal) -> WebOutcome:
        """The outcome of a call that reached no search provider."""
        if not reason.planned:
            return cls(
                receipt=f"Privacy receipt. Nothing left this machine, because {reason.because}."
            )
        return cls(
            receipt=(
                f"Privacy receipt. {cls.planning()}, and no search provider was contacted, "
                f"because {reason.because}."
            )
        )

    @classmethod
    def fruitless(
        cls,
        reason: Refusal,
        query: str,
        lane: SearchLane,
        providers: tuple[str, ...],
    ) -> WebOutcome:
        """The outcome of a call that did contact providers and came back with nothing.

        Reaching a provider and getting nothing is still reaching a provider. A receipt that
        reported this as though the machine had stayed quiet would understate the egress by
        exactly the calls that were made.
        """
        return cls(
            receipt=(
                f"Privacy receipt. {cls.planning()}, and the rewritten question `{query}` was "
                f"sent to the {lane.value} lane through "
                f"{', '.join(f'`{name}`' for name in providers)}, which returned nothing "
                f"usable, because {reason.because}."
            )
        )

    @classmethod
    def sent(
        cls,
        findings: tuple[WebFinding, ...],
        query: str,
        lane: SearchLane,
        providers: tuple[str, ...],
    ) -> WebOutcome:
        """The outcome of a call that did reach the public web, naming exactly what went."""
        return cls(
            findings=findings,
            receipt=(
                f"Privacy receipt. {cls.planning()}. Only the rewritten question `{query}` "
                f"reached the {lane.value} lane through "
                f"{', '.join(f'`{name}`' for name in providers)}, and it carries nothing that "
                "identifies you."
            ),
        )
