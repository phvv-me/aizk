import json
from pathlib import Path

import pytest
from id_factory import uuid7

from aizk.provenance import Stance
from aizk.retrieval import Candidate, Lane
from aizk.retrieval.packing import deduplicate
from eval.certainty import (
    CERTAINTY_CASES_PATH,
    CertaintyCase,
    CertaintyOutcome,
    CertaintyReport,
    detector_reading,
    load_certainty_cases,
    measure_certainty,
)

CASES = load_certainty_cases(CERTAINTY_CASES_PATH)


def test_every_committed_case_is_adversarial_by_construction() -> None:
    # the corpus is only worth reading if each case already passes quote verification, so
    # nothing in it is caught by the check that existed before
    assert len(CASES) == 46
    assert all(case.quote in case.sentence for case in CASES)
    assert sum(case.distorting for case in CASES) == 17


def test_the_change_is_measured_rather_than_asserted() -> None:
    report = measure_certainty()

    # every flattening the old pipeline stored, because a contiguous exact quote was proof
    assert report.flattening_admitted_before == 1.0
    assert report.false_rejection_before == 0.0
    assert report.source_paired_before == 0.0
    # what the gate does now, and what it costs. These are the numbers to argue with: if a
    # lexicon change moves them, this test says so instead of letting the claim drift.
    #
    # The refuted register grew five constructions (a copula before `invalid`/`obsolete`,
    # `no longer valid`, `turned out to be false`, `debunked`) to close the supersession
    # defect, so 14 cases joined the corpus: 5 flattened/preserved pairs exercising each new
    # construction plus 4 adversarial negatives (`invalid input`, `false positive rate`, an
    # `obsolete API`, a `void` return type) proving none of them fires on ordinary technical
    # prose. Both guarded rates fall, both denominators grow by exactly the new cases, and
    # both moves are in the direction the fix intends: flattening_admitted 1/12 -> 1/17,
    # because the five new flattenings are now correctly rejected; false_rejection
    # 1/20 -> 1/29, because the five new preserved cases and four negatives are all still
    # admitted; settledness_accuracy 31/32 -> 45/46, because the new register reads every
    # one of the fourteen new sentences correctly and dilutes the one pre-existing miss.
    assert report.flattening_admitted == pytest.approx(1 / 17)
    assert report.false_rejection == pytest.approx(1 / 29)
    assert report.settledness_accuracy == pytest.approx(45 / 46)
    assert report.source_paired == 1.0
    assert report.distortion_labelled == 1.0


def test_the_one_flattening_still_admitted_is_the_labelled_attribution() -> None:
    # a dropped attribution keeps the claim intact, so it is labelled rather than refused.
    # Published work finds the evidential register barely discounted by readers, which is
    # why the excerpt is paired with it rather than the label being trusted alone.
    report = measure_certainty()
    admitted = [
        outcome for outcome in report.outcomes if outcome.admitted and not outcome.preserves
    ]

    assert [outcome.id for outcome in admitted] == ["attribution-flattened"]
    assert admitted[0].stored is Stance.reported
    assert admitted[0].paired_with_source and not admitted[0].paired_before


def test_the_one_faithful_case_refused_is_a_qualifier_read_too_widely() -> None:
    report = measure_certainty()
    refused = [
        outcome for outcome in report.outcomes if outcome.preserves and not outcome.admitted
    ]

    # a conditional elsewhere in the sentence is read as qualifying this claim too. Dropping
    # `if` from the lexicon removes this at the cost of admitting a flattened conditional
    # unlabelled, which is the worse of the two, so the cost is paid deliberately.
    assert [outcome.id for outcome in refused] == ["temporal-if-elsewhere"]


def test_the_incident_that_started_this_is_a_regression_case() -> None:
    report = measure_certainty()
    incident = {outcome.id: outcome for outcome in report.outcomes}

    assert incident["v7-margin-flattened"].admitted_before
    assert not incident["v7-margin-flattened"].admitted
    assert incident["v7-margin-preserved"].admitted
    assert incident["v7-margin-preserved"].stored is Stance.hedged
    assert incident["v7-summary-flattened"].admitted_before
    assert not incident["v7-summary-flattened"].admitted


def test_a_derived_fact_never_outranks_the_excerpt_it_was_drawn_from() -> None:
    """`docs/user/concepts/sources.md` states that the source wins when the two disagree.

    A derived claim that flattened its source is exactly a disagreement, so the excerpt
    carrying the sentence behind it has to survive the packing walk that would otherwise
    treat it as repetition. This test fails if that rule goes back to being advice.
    """
    chunk = uuid7()
    excerpt = Candidate(
        lane=Lane.Kind.SOURCES,
        line="V7 improves over the baseline, though within run-to-run variance",
        source_chunk_id=chunk,
    )
    unsettled = [
        Candidate(
            lane=Lane.Kind.FACTS,
            line="- the derived claim",
            source_chunk_id=chunk,
            stance=stance,
        )
        for stance in Stance
        if stance is not Stance.settled
    ]

    assert all(excerpt in deduplicate([claim, excerpt]) for claim in unsettled)


def test_a_corpus_of_one_faithful_case_scores_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    case = CertaintyCase(
        id="lone",
        sentence="Aizk keeps every claim in PostgreSQL.",
        quote="Aizk keeps every claim in PostgreSQL",
        statement="Aizk keeps every claim in PostgreSQL.",
        expressed=Stance.settled,
        preserves=True,
    )
    path.write_text(f"{case.model_dump_json()}\n\n", encoding="utf-8")

    report = measure_certainty(path)

    # with no distorting and no unsettled case the guarded rates have nothing to divide by
    assert (report.cases, report.distorting, report.faithful) == (1, 0, 1)
    assert report.flattening_admitted == 0.0
    assert report.source_paired == 0.0
    assert report.distortion_labelled == 1.0
    assert report.render().splitlines()[0] == "certainty n=1 distorting=0 faithful=1"


def test_a_case_whose_quote_does_not_ground_is_reported_rather_than_scored() -> None:
    ungrounded = CertaintyCase(
        id="ungrounded",
        sentence="Aizk keeps every claim in PostgreSQL.",
        quote="nowhere in this sentence",
        statement="Aizk keeps every claim elsewhere.",
        expressed=Stance.settled,
        preserves=True,
    )

    outcome = CertaintyOutcome.measure(ungrounded)
    rendered = CertaintyReport.score([outcome]).render()

    assert not outcome.admitted and not outcome.admitted_before
    assert "ungrounded" in rendered and "misses" in rendered


def test_the_detector_reading_is_exposed_for_authoring_a_case() -> None:
    assert detector_reading("the margin may be inside variance") is Stance.hedged
    assert detector_reading("the margin is 4.2 points") is Stance.settled


def test_the_committed_corpus_is_valid_jsonl() -> None:
    lines = [
        line for line in CERTAINTY_CASES_PATH.read_text(encoding="utf-8").splitlines() if line
    ]

    assert all(json.loads(line)["id"] for line in lines)
    assert len({json.loads(line)["id"] for line in lines}) == len(lines)
