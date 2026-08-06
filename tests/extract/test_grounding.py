import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.extract.models import ExtractedEntity, Extraction, TimedFact
from aizk.graph.grounding import (
    GroundedProjection,
    Qualification,
    certainty,
    quote_interval,
    sentence_around,
)
from aizk.provenance import Stance

body = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)),
    min_size=1,
    max_size=200,
)


@given(text=body, data=st.data())
def test_an_exact_substring_recovers_its_own_span(text: str, data: st.DataObject) -> None:
    start = data.draw(st.integers(min_value=0, max_value=len(text) - 1))
    end = data.draw(st.integers(min_value=start + 1, max_value=len(text)))
    quote = text[start:end].strip()
    if not quote:
        return

    interval = quote_interval(quote, text)

    assert interval is not None
    found_start, found_end = interval
    assert text[found_start:found_end] == quote


def canonical(value: str) -> str:
    """Collapse the presentation grounding ignores, meaning case, whitespace and backticks."""
    return re.sub(r"\s+", " ", value.replace("`", "").casefold()).strip()


@given(text=body)
def test_a_case_and_whitespace_mangled_quote_still_aligns(text: str) -> None:
    words = [word for word in re.split(r"\s+", text) if word]
    if len(words) < 2:
        return
    mangled = "  ".join(word.upper() for word in words[:2])

    interval = quote_interval(mangled, text)

    if interval is None:
        return
    start, end = interval
    assert canonical(text[start:end]) == canonical(mangled)


@pytest.mark.parametrize("quote", [None, "", "   ", "`", "never appears anywhere"])
def test_absent_or_unfindable_quotes_ground_nothing(quote: str | None) -> None:
    assert quote_interval(quote, "some entirely unrelated text") is None


def test_whitespace_variants_map_back_to_source_offsets() -> None:
    text = "The  compression   engine\nuses the Leech lattice."
    quote = "compression engine uses"

    interval = quote_interval(quote, text)

    assert interval is not None
    start, end = interval
    assert text[start:end] == "compression   engine\nuses"


def test_markdown_backticks_do_not_hide_verbatim_evidence() -> None:
    text = "The public `remember` tool stopped accepting `kind`."
    quote = "The public remember tool stopped accepting kind"

    interval = quote_interval(quote, text)

    assert interval is not None
    start, end = interval
    assert text[start:end].replace("`", "") == quote


def test_projection_accepts_only_grounded_facts_with_canonical_endpoints() -> None:
    source = "Aizk uses PostgreSQL and keeps source evidence."
    projection = GroundedProjection.from_extraction(
        Extraction(
            entities=[
                ExtractedEntity(name="Aizk", type="tool"),
                ExtractedEntity(name="PostgreSQL", type="tool"),
                ExtractedEntity(name="Unused", type="concept"),
                ExtractedEntity(name=" ", type="concept"),
            ],
            facts=[
                TimedFact(
                    subject="aizk",
                    predicate="uses",
                    object="postgresql",
                    statement="Aizk uses PostgreSQL.",
                    quote="Aizk uses PostgreSQL",
                )
            ],
        ),
        source,
    )

    assert [entity.name for entity in projection.entities] == ["Aizk", "PostgreSQL"]
    assert projection.facts[0].subject == "Aizk"
    assert projection.facts[0].object_ == "PostgreSQL"
    assert projection.quality.accepted_facts == 1
    assert projection.quality.rejected_facts == 0


def test_projection_rejects_path_like_endpoints_before_graph_writing() -> None:
    source = "Aizk reads deploy/logto.conf."
    projection = GroundedProjection.from_extraction(
        Extraction(
            entities=[
                ExtractedEntity(name="Aizk", type="tool"),
                ExtractedEntity(name="deploy/logto.conf", type="document"),
            ],
            facts=[
                TimedFact(
                    subject="Aizk",
                    predicate="reads",
                    object="deploy/logto.conf",
                    statement="Aizk reads its Logto configuration.",
                    quote=source,
                )
            ],
        ),
        source,
    )

    assert projection.facts == []
    assert projection.quality.unresolved_endpoint == 1


def test_projection_reports_every_deterministic_rejection_reason() -> None:
    source = "Aizk uses PostgreSQL."
    extraction = Extraction(
        entities=[
            ExtractedEntity(name="Aizk", type="tool"),
            ExtractedEntity(name="PostgreSQL", type="tool"),
        ],
        facts=[
            TimedFact(subject="Aizk", predicate="uses", statement="missing"),
            TimedFact(
                subject="Aizk",
                predicate="uses",
                statement="invented",
                quote="not in the source",
            ),
            TimedFact(
                subject="Ghost",
                predicate="uses",
                statement="unresolved",
                quote="Aizk uses PostgreSQL",
            ),
            TimedFact(
                subject="Aizk",
                predicate="uses",
                object="Aizk",
                statement="self",
                quote="Aizk uses PostgreSQL",
            ),
            TimedFact(
                subject="Aizk",
                predicate="related_to",
                object="PostgreSQL",
                statement="generic",
                quote="Aizk uses PostgreSQL",
            ),
        ],
    )
    projection = GroundedProjection.from_extraction(extraction, source)
    audit = GroundedProjection.audit(extraction, source)

    assert projection.facts == []
    assert projection.entities == []
    assert [item.rejection for item in audit] == [
        "missing_quote",
        "unsupported_quote",
        "unresolved_endpoint",
        "self_relation",
        "generic_relation",
    ]
    assert audit[1].fact.statement == "invented"
    assert projection.quality.model_dump() == {
        "proposed_entities": 2,
        "accepted_entities": 0,
        "proposed_facts": 5,
        "accepted_facts": 0,
        "missing_quote": 1,
        "unsupported_quote": 1,
        "stripped_qualifier": 0,
        "unresolved_endpoint": 1,
        "self_relation": 1,
        "generic_relation": 1,
    }
    assert projection.quality.rejected_facts == 5


# The reported incident. A collaborator's hedged research note whose confident half is a
# contiguous, character-exact substring, which is exactly why quote verification let the
# flattened reading through.
HEDGED = (
    "The ablation is encouraging. RIRobustNetV7 outperforms RIConv++ on noise, though the "
    "margin falls within run-to-run variance. We report it for completeness."
)


def proposed(quote: str, statement: str, stance: Stance = Stance.settled) -> TimedFact:
    """One model-proposed fact carrying the quote and statement under test."""
    return TimedFact(
        subject="RIRobustNetV7",
        predicate="outperforms",
        object="RIConv++",
        statement=statement,
        quote=quote,
        stance=stance,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Aizk keeps every claim in PostgreSQL", Stance.settled),
        ("Smith reports that the margin holds", Stance.reported),
        ("the margin holds, though only on synthetic data", Stance.hedged),
        ("the two teams disagree on the margin", Stance.disputed),
        ("the earlier margin claim is refuted", Stance.refuted),
        # the least settled register present decides, never the first one found
        ("Smith reports that the refuted margin may hold", Stance.refuted),
    ],
)
def test_certainty_reads_the_least_settled_register_a_span_speaks_in(
    text: str, expected: Stance
) -> None:
    assert certainty(text) is expected


def test_a_quote_expands_to_the_sentence_that_governs_it() -> None:
    start = HEDGED.index("RIRobustNetV7")
    end = start + len("RIRobustNetV7 outperforms RIConv++ on noise")

    sentence = sentence_around(HEDGED, start, end)

    assert sentence.strip().startswith("RIRobustNetV7 outperforms")
    assert sentence.strip().endswith("run-to-run variance.")


def test_a_span_with_no_boundary_on_either_side_is_its_own_sentence() -> None:
    assert sentence_around("no terminator here", 3, 5) == "no terminator here"


def test_the_flattened_hedge_is_rejected_rather_than_stored_as_a_settled_fact() -> None:
    # the exact defect: character-exact contiguous quote, meaning inverted
    fact = proposed(
        "RIRobustNetV7 outperforms RIConv++ on noise",
        "RIRobustNetV7 beats RIConv++ on noise and occlusion.",
    )

    qualification = Qualification.read(fact, HEDGED)

    assert quote_interval(fact.quote, HEDGED) is not None
    assert (qualification.source, qualification.expressed) == (Stance.hedged, Stance.settled)
    assert qualification.stripped


def test_a_quote_spanning_its_qualifier_survives_and_can_never_read_as_settled() -> None:
    fact = proposed(
        "RIRobustNetV7 outperforms RIConv++ on noise, though the margin falls within "
        "run-to-run variance",
        "RIRobustNetV7 outperforms RIConv++ on noise, though within run-to-run variance.",
    )

    qualification = Qualification.read(fact, HEDGED)

    assert not qualification.stripped
    assert qualification.settledness(Stance.settled) is Stance.hedged


def test_a_qualifier_reflected_only_in_the_statement_still_survives() -> None:
    # the quote stops short, but the fact itself carries what the source qualified
    fact = proposed(
        "RIRobustNetV7 outperforms RIConv++ on noise",
        "RIRobustNetV7 outperforms RIConv++ on noise, though the margin is inconclusive.",
    )

    assert not Qualification.read(fact, HEDGED).stripped


def test_a_dropped_negation_is_the_same_distortion_reached_the_other_way() -> None:
    source = "The audit does not show that RIRobustNetV7 beats RIConv++."
    fact = proposed("RIRobustNetV7 beats RIConv++", "RIRobustNetV7 beats RIConv++.")

    assert Qualification.read(fact, source).inverted
    assert Qualification.read(fact, source).stripped


def test_a_negation_the_fact_keeps_is_a_settled_claim_rather_than_a_hedge() -> None:
    source = "Aizk does not load model weights in its own process."
    fact = proposed(
        "Aizk does not load model weights",
        "Aizk does not load model weights in its own process.",
    )

    qualification = Qualification.read(fact, source)

    assert not qualification.stripped
    assert qualification.settledness(Stance.settled) is Stance.settled


def test_losing_an_attribution_labels_the_fact_rather_than_dropping_it() -> None:
    # the claim itself survives intact, so the stance plus the excerpt carry what was lost
    source = "Smith reports that RIRobustNetV7 beats RIConv++."
    fact = proposed("RIRobustNetV7 beats RIConv++", "RIRobustNetV7 beats RIConv++.")

    qualification = Qualification.read(fact, source)

    assert not qualification.stripped
    assert qualification.settledness(Stance.settled) is Stance.reported


def test_extraction_may_read_a_source_as_less_settled_but_never_as_more() -> None:
    fact = proposed("RIRobustNetV7 outperforms RIConv++ on noise", "x", stance=Stance.disputed)

    qualification = Qualification.read(fact, "RIRobustNetV7 outperforms RIConv++ on noise.")

    assert qualification.settledness(fact.stance) is Stance.disputed


def test_a_fact_whose_quote_does_not_ground_qualifies_nothing() -> None:
    fact = proposed("nowhere in the source", "unrelated")

    assert Qualification.read(fact, HEDGED) == Qualification()


def test_a_correcting_sentence_marks_the_fact_and_the_others_do_not() -> None:
    corrected = proposed(
        "the earlier margin claim is refuted",
        "The earlier margin claim is refuted.",
    )

    assert Qualification.read(
        corrected, "In part 3, the earlier margin claim is refuted."
    ).correcting
    assert not Qualification.read(
        proposed("Aizk uses PostgreSQL", "Aizk uses PostgreSQL."), "Aizk uses PostgreSQL."
    ).correcting


def test_the_projection_refuses_a_flattened_hedge_and_stamps_what_it_keeps() -> None:
    entities = [
        ExtractedEntity(name="RIRobustNetV7", type="tool"),
        ExtractedEntity(name="RIConv++", type="tool"),
    ]
    flattened = proposed(
        "RIRobustNetV7 outperforms RIConv++ on noise",
        "RIRobustNetV7 beats RIConv++ on noise and occlusion.",
    )
    faithful = proposed(
        "RIRobustNetV7 outperforms RIConv++ on noise, though the margin falls within "
        "run-to-run variance",
        "RIRobustNetV7 outperforms RIConv++ on noise, though inconclusively.",
    )

    rejected = GroundedProjection.from_extraction(
        Extraction(entities=entities, facts=[flattened]), HEDGED
    )
    kept = GroundedProjection.from_extraction(
        Extraction(entities=entities, facts=[faithful]), HEDGED
    )

    assert rejected.facts == []
    assert rejected.quality.stripped_qualifier == 1
    assert [
        item.rejection
        for item in GroundedProjection.audit(
            Extraction(entities=entities, facts=[flattened]), HEDGED
        )
    ] == ["stripped_qualifier"]
    assert kept.facts[0].stance is Stance.hedged
    assert kept.facts[0].correcting is False
