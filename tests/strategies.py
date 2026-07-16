from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hypothesis import strategies as st
from id_factory import uuid7s
from sqlalchemy.dialects.postgresql import Range

from aizk.config import Settings
from aizk.extract.models import ConsolidationVerdict, ExtractedEntity, Extraction, TimedFact
from aizk.ontology import WireEntity, WireExtraction, WireFact
from aizk.retrieval import Candidate, Lane

# Fixed readable values keep collection independent of the live ontology.
_ENTITY_TYPES = ("concept", "decision", "pattern", "project", "paper", "tool")
_PREDICATES = ("related_to", "because", "depends_on", "cites", "uses")

_entity_types = st.sampled_from(_ENTITY_TYPES)
predicates = st.sampled_from(_PREDICATES)

# Printable text keeps shrunk failures readable.
short_text = st.text(min_size=1, max_size=40)

_aware_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(UTC),
)


def vector(width: int = Settings().embed_dim) -> st.SearchStrategy[list[float]]:
    return st.lists(
        st.floats(allow_nan=False, allow_infinity=False, width=16),
        min_size=width,
        max_size=width,
    )


def vectors(width: int = Settings().embed_dim) -> st.SearchStrategy[list[list[float]]]:
    return st.lists(vector(width), min_size=0, max_size=4)


def candidates() -> st.SearchStrategy[Candidate]:
    return st.builds(
        Candidate,
        lane=st.sampled_from(list(Lane.Kind)),
        line=short_text,
        source_title=st.none() | short_text,
        source_uri=st.none() | short_text,
    )


@st.composite
def recalled_candidates(draw: st.DrawFn) -> tuple[Candidate, ...]:
    return tuple(draw(st.lists(candidates(), max_size=12)))


def extracted_entities() -> st.SearchStrategy[ExtractedEntity]:
    return st.builds(ExtractedEntity, name=short_text, type=_entity_types, attributes=st.just({}))


def wire_entities() -> st.SearchStrategy[WireEntity]:
    return st.builds(WireEntity, n=short_text, t=st.sampled_from(_ENTITY_TYPES))


def wire_facts() -> st.SearchStrategy[WireFact]:
    return st.builds(
        WireFact,
        s=short_text,
        p=predicates,
        o=st.just("") | short_text,
        statement=short_text,
        date=st.none() | short_text,
    )


def wire_extractions() -> st.SearchStrategy[WireExtraction]:
    return st.builds(
        WireExtraction,
        e=st.lists(wire_entities(), max_size=4),
        f=st.lists(wire_facts(), max_size=4),
    )


def extractions() -> st.SearchStrategy[Extraction]:
    return st.builds(
        Extraction,
        entities=st.lists(extracted_entities(), max_size=4),
        facts=st.lists(timed_facts(), max_size=4),
    )


def timed_facts() -> st.SearchStrategy[TimedFact]:
    return st.builds(
        TimedFact,
        subject=short_text,
        predicate=predicates,
        statement=short_text,
        valid_from=st.none() | _aware_datetimes,
        valid_to=st.none() | _aware_datetimes,
    )


def consolidation_verdicts() -> st.SearchStrategy[ConsolidationVerdict]:
    return st.one_of(
        st.builds(ConsolidationVerdict, action=st.sampled_from(["ADD", "NOOP"])),
        st.builds(
            ConsolidationVerdict,
            action=st.just("UPDATE"),
            supersedes=st.none() | uuid7s,
        ),
    )


@dataclass(frozen=True, slots=True)
class TemporalState:
    is_latest: bool
    valid_from_days: float | None
    valid_to_days: float | None

    def window(self, now: datetime) -> tuple[datetime | None, datetime | None]:
        start = (
            None if self.valid_from_days is None else now + timedelta(days=self.valid_from_days)
        )
        end = None if self.valid_to_days is None else now + timedelta(days=self.valid_to_days)
        return start, end

    def valid(self, now: datetime) -> Range[datetime]:
        start, end = self.window(now)
        if start is not None and end is not None and start > end:
            return Range(empty=True)
        return Range(start, end)

    def recorded(self, now: datetime) -> Range[datetime]:
        return Range(now - timedelta(days=1), None if self.is_latest else now)

    def expected_current(self, now: datetime) -> bool:
        start, end = self.window(now)
        return self.is_latest and (start is None or start <= now) and (end is None or end > now)


_offsets = st.none() | st.floats(
    min_value=-400.0, max_value=400.0, allow_nan=False, allow_infinity=False
).filter(lambda days: abs(days) > 1.0)


def temporal_states() -> st.SearchStrategy[TemporalState]:
    return st.builds(
        TemporalState,
        is_latest=st.booleans(),
        valid_from_days=_offsets,
        valid_to_days=_offsets,
    )


@st.composite
def fact_timeline(draw: st.DrawFn, max_versions: int = 4) -> tuple[list[TemporalState], datetime]:
    count = draw(st.integers(min_value=1, max_value=max_versions))
    latest_index = draw(st.integers(min_value=0, max_value=count - 1))
    states = [
        TemporalState(
            is_latest=index == latest_index,
            valid_from_days=draw(_offsets),
            valid_to_days=draw(_offsets),
        )
        for index in range(count)
    ]
    probe = draw(_aware_datetimes)
    return states, probe
