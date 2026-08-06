import json
import uuid
from collections.abc import Sequence
from pathlib import Path

from patos import FrozenModel

from aizk.extract.models import ExtractedEntity, Extraction, TimedFact
from aizk.graph.grounding import GroundedProjection, Qualification, certainty
from aizk.provenance import Stance
from aizk.retrieval import Candidate, Lane
from aizk.retrieval.packing import deduplicate

from .metrics import ratio

CERTAINTY_CASES_PATH = (
    Path(__file__).resolve().parents[2] / "tests/eval/data/certainty_cases.jsonl"
)


class CertaintyCase(FrozenModel):
    """One source sentence and a fact a model plausibly proposes from it.

    `quote` is always a contiguous, character-exact substring of `sentence`, which is what
    makes this corpus adversarial: every case passes quote verification, so any that is
    caught is caught by reading the sentence around the quote rather than by the quote
    check that already existed.
    """

    id: str
    sentence: str
    quote: str
    statement: str
    expressed: Stance
    preserves: bool

    @property
    def distorting(self) -> bool:
        """Whether accepting this proposal as written would overstate its own source."""
        return not self.preserves

    def proposed(self) -> Extraction:
        """The extraction a model would hand grounding for this case."""
        return Extraction(
            entities=[
                ExtractedEntity(name="Subject", type="concept"),
                ExtractedEntity(name="Object", type="concept"),
            ],
            facts=[
                TimedFact(
                    subject="Subject",
                    predicate="improves_over",
                    object="Object",
                    statement=self.statement,
                    quote=self.quote,
                )
            ],
        )


class CertaintyOutcome(FrozenModel):
    """What the deterministic gate did with one case, under both pipelines."""

    id: str
    expressed: Stance
    preserves: bool
    read: Stance
    stored: Stance | None
    admitted: bool
    admitted_before: bool
    paired_with_source: bool
    paired_before: bool

    @classmethod
    def measure(cls, case: CertaintyCase) -> CertaintyOutcome:
        """Run one case through the real grounding audit and the real packing walk.

        The before arm is this same audit without the certainty comparison, which is exactly
        what the code did prior to the change: a contiguous character-exact quote was proof
        enough. Nothing is simulated, the two arms differ only by that one rule.
        """
        extraction = case.proposed()
        [audited] = GroundedProjection.audit(extraction, case.sentence)
        projection = GroundedProjection.from_extraction(extraction, case.sentence)
        qualification = Qualification.read(extraction.facts[0], case.sentence)
        stored = projection.facts[0].stance if projection.facts else None
        return cls(
            id=case.id,
            expressed=case.expressed,
            preserves=case.preserves,
            read=qualification.source,
            stored=stored,
            admitted=bool(projection.facts),
            admitted_before=audited.rejection in (None, "stripped_qualifier"),
            paired_with_source=paired(qualification.settledness(Stance.settled)),
            paired_before=paired(Stance.settled),
        )


def paired(stance: Stance) -> bool:
    """Whether recall would still return the excerpt a derived claim at `stance` came from.

    Measured through the production packing walk with the derived fact ranked above its own
    grounding excerpt, which is the arrangement `docs/user/concepts/sources.md` says must
    leave the source in charge.
    """
    chunk = uuid.uuid7()
    fact = Candidate(
        lane=Lane.Kind.FACTS, line="- the derived claim", source_chunk_id=chunk, stance=stance
    )
    excerpt = Candidate(lane=Lane.Kind.SOURCES, line="the source sentence", source_chunk_id=chunk)
    return excerpt in deduplicate([fact, excerpt])


class CertaintyReport(FrozenModel):
    """Whether the change preserves the certainty of a source, and what it costs.

    Three numbers decide it. `flattening_admitted` is how often a proposal that overstates
    its source is stored anyway, measured before and after. `false_rejection` is how often a
    faithful proposal is refused, the price paid for that. `source_paired` is whether an
    unsettled claim actually changes what recall returns rather than only what it says.
    """

    cases: int
    distorting: int
    faithful: int
    flattening_admitted_before: float
    flattening_admitted: float
    false_rejection_before: float
    false_rejection: float
    distortion_labelled: float
    settledness_accuracy: float
    source_paired_before: float
    source_paired: float
    outcomes: tuple[CertaintyOutcome, ...]

    @classmethod
    def score(cls, outcomes: Sequence[CertaintyOutcome]) -> CertaintyReport:
        """Aggregate per-case outcomes into the before and after rates."""
        distorting = [outcome for outcome in outcomes if not outcome.preserves]
        faithful = [outcome for outcome in outcomes if outcome.preserves]
        unsettled = [outcome for outcome in outcomes if outcome.expressed is not Stance.settled]
        return cls(
            cases=len(outcomes),
            distorting=len(distorting),
            faithful=len(faithful),
            flattening_admitted_before=ratio(
                sum(outcome.admitted_before for outcome in distorting), len(distorting)
            ),
            flattening_admitted=ratio(
                sum(outcome.admitted for outcome in distorting), len(distorting)
            ),
            false_rejection_before=ratio(
                sum(not outcome.admitted_before for outcome in faithful), len(faithful)
            ),
            false_rejection=ratio(
                sum(not outcome.admitted for outcome in faithful), len(faithful)
            ),
            # of the overstating proposals still stored, how many at least carry a stance
            # saying so. This is the fallback for the registers the gate keeps rather than
            # rejects, and on its own it is only as good as a reader who reads labels.
            distortion_labelled=ratio(
                sum(
                    outcome.stored is not Stance.settled
                    for outcome in distorting
                    if outcome.admitted
                ),
                sum(outcome.admitted for outcome in distorting),
                1.0,
            ),
            settledness_accuracy=ratio(
                sum(outcome.read is outcome.expressed for outcome in outcomes), len(outcomes), 1.0
            ),
            source_paired_before=ratio(
                sum(outcome.paired_before for outcome in unsettled), len(unsettled)
            ),
            source_paired=ratio(
                sum(outcome.paired_with_source for outcome in unsettled), len(unsettled)
            ),
            outcomes=tuple(outcomes),
        )

    def render(self) -> str:
        """Render the before and after scorecard and every case the gate still gets wrong."""
        summary = (
            f"certainty n={self.cases} distorting={self.distorting} faithful={self.faithful}\n"
            f"  flattening admitted  {self.flattening_admitted_before:.3f} -> "
            f"{self.flattening_admitted:.3f}\n"
            f"  false rejection      {self.false_rejection_before:.3f} -> "
            f"{self.false_rejection:.3f}\n"
            f"  distortion labelled  {self.distortion_labelled:.3f}\n"
            f"  source paired        {self.source_paired_before:.3f} -> {self.source_paired:.3f}\n"
            f"  settledness accuracy {self.settledness_accuracy:.3f}"
        )
        misses = "\n".join(
            f"  {outcome.id} expressed={outcome.expressed} read={outcome.read} "
            f"stored={outcome.stored} admitted={outcome.admitted}"
            for outcome in self.outcomes
            if outcome.admitted is not outcome.preserves or outcome.read is not outcome.expressed
        )
        return f"{summary}\nmisses\n{misses}" if misses else summary


def load_certainty_cases(path: Path) -> tuple[CertaintyCase, ...]:
    """Read one JSONL certainty corpus, skipping blank lines."""
    return tuple(
        CertaintyCase.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def measure_certainty(path: Path = CERTAINTY_CASES_PATH) -> CertaintyReport:
    """Score the committed certainty corpus with no model and no database.

    The gate under test is entirely deterministic, so this runs anywhere and its numbers are
    exactly reproducible, which is what makes a before and after comparison worth reading.
    """
    return CertaintyReport.score(
        [CertaintyOutcome.measure(case) for case in load_certainty_cases(path)]
    )


def detector_reading(sentence: str) -> Stance:
    """The certainty one sentence expresses, exposed for corpus authoring and diagnosis."""
    return certainty(sentence)
