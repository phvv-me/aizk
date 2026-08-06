import pytest
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5

from aizk.provenance import CaptureContext, EpistemicKind, Stance

stances = st.sampled_from(list(Stance))


def test_the_ladder_runs_from_settled_to_refuted_in_declaration_order() -> None:
    assert [stance.value for stance in sorted(Stance, key=lambda item: item.rank)] == [
        "settled",
        "reported",
        "hedged",
        "disputed",
        "refuted",
    ]


@given(stance=stances, floor=stances)
def test_evidence_about_a_claim_may_only_ever_lower_confidence_in_it(
    stance: Stance, floor: Stance
) -> None:
    settled = stance.at_least(floor)

    assert settled.rank >= max(stance.rank, floor.rank)
    assert settled in {stance, floor}
    # applying a floor twice cannot drift, and a floor of settled changes nothing
    assert settled.at_least(floor) is settled
    assert stance.at_least(Stance.settled) is stance


@pytest.mark.parametrize(
    ("stance", "distorting", "decisive"),
    [
        (Stance.settled, False, True),
        (Stance.reported, False, False),
        (Stance.hedged, True, False),
        (Stance.disputed, True, False),
        (Stance.refuted, True, True),
    ],
)
def test_a_stance_says_both_what_reading_it_flatly_would_cost_and_what_it_may_close(
    stance: Stance, distorting: bool, decisive: bool
) -> None:
    # distorting drives rejection at extraction, decisive drives retraction at consolidation
    assert stance.distorting is distorting
    assert stance.decisive is decisive


def test_the_two_axes_stay_independent_in_what_a_claim_records() -> None:
    speaker = uuid5()
    capture = CaptureContext(speaker_label="Maya", client="probe/1.2.3")

    attributes = capture.claim_attributes(EpistemicKind.observation, speaker)

    # whose claim it is rides on the claim, how settled it is is stamped by the writer
    assert attributes["epistemic_kind"] == "observation"
    assert attributes["perspective_key"] == f"speaker:{speaker}"
    assert attributes["client"] == "probe/1.2.3"
    assert "stance" not in attributes
