import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.extract.models import ExtractedEntity, Extraction, TimedFact
from aizk.graph.grounding import GroundedProjection, quote_interval

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
        "unresolved_endpoint": 1,
        "self_relation": 1,
        "generic_relation": 1,
    }
    assert projection.quality.rejected_facts == 5
