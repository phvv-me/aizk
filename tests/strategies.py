from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from hypothesis import strategies as st
from sqlalchemy.dialects.postgresql import Range

from aizk.config import Settings
from aizk.extract import ontology
from aizk.extract.models import ConsolidationVerdict, ExtractedEntity, Extraction, TimedFact
from aizk.retrieval import (
    CommunityNote,
    FactHit,
    Hit,
    QueryRoute,
    RaptorNote,
    RecallResult,
    RoutePlan,
    SessionNote,
)


class WireEntity(Protocol):
    """The shape of one entity in the live, dynamically-built combined extraction wire schema,
    `ontology.current().llm_entity` structurally, since the real class is rebuilt fresh on every
    `ontology.refresh` and so cannot be named directly in a type annotation."""

    n: str
    t: str


class WireFact(Protocol):
    """The shape of one fact in the live wire schema, `ontology.current().llm_fact`
    structurally."""

    s: str
    p: str
    o: str


class WireExtraction(Protocol):
    """The shape of the combined extraction call's wire schema, `ontology.current().
    llm_extraction` structurally."""

    e: list[WireEntity]
    f: list[WireFact]


# a small, fixed pool of real seeded catalog names, deliberately not read from `ontology.
# current()`, these strategies are drawn at collection time by test modules that never touch the
# database, so they cannot depend on `ops.setup()` having run. `ExtractedEntity.type`/`TimedFact.
# predicate` are plain `str` fields, any value works, this pool only keeps examples readable.
ENTITY_TYPE_LIST = ("concept", "decision", "pattern", "project", "paper", "tool")
PREDICATE_LIST = ("related_to", "because", "depends_on", "cites", "uses")

entity_types = st.sampled_from(ENTITY_TYPE_LIST)
predicates = st.sampled_from(PREDICATE_LIST)

# short non-empty surface text for names, statements, and snippets, kept printable so a failing
# example reads cleanly.
short_text = st.text(min_size=1, max_size=40)

# finite scores, the fused relevance values every result model carries.
scores = st.floats(allow_nan=False, allow_infinity=False, allow_subnormal=False)

# timezone-aware instants, the only datetimes the bi-temporal columns store.
aware_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(UTC),
)


def vector(width: int = Settings().embed_dim) -> st.SearchStrategy[list[float]]:
    """A single embedding row of exactly `width` finite floats."""
    return st.lists(
        st.floats(allow_nan=False, allow_infinity=False, width=16),
        min_size=width,
        max_size=width,
    )


def vectors(width: int = Settings().embed_dim) -> st.SearchStrategy[list[list[float]]]:
    """A batch of correctly-sized embedding rows, the happy input `validated` returns unchanged."""
    return st.lists(vector(width), min_size=0, max_size=4)


def hits() -> st.SearchStrategy[Hit]:
    """One fused chunk hit with optional provenance."""
    return st.builds(
        Hit,
        document_title=st.none() | short_text,
        source_uri=st.none() | short_text,
        text=short_text,
        score=scores,
    )


def fact_hits() -> st.SearchStrategy[FactHit]:
    """One time-stamped graph hit over the closed predicate vocabulary."""
    return st.builds(
        FactHit,
        statement=short_text,
        predicate=predicates,
        score=scores,
        valid_from=st.none() | aware_datetimes,
        valid_to=st.none() | aware_datetimes,
    )


def community_notes() -> st.SearchStrategy[CommunityNote]:
    """One community summary surfaced for a thematic query."""
    return st.builds(CommunityNote, label=short_text, summary=short_text, score=scores)


def raptor_notes() -> st.SearchStrategy[RaptorNote]:
    """One RAPTOR tier summary at some level."""
    return st.builds(
        RaptorNote,
        label=short_text,
        summary=short_text,
        level=st.integers(min_value=0, max_value=8),
        score=scores,
    )


def session_notes() -> st.SearchStrategy[SessionNote]:
    """One still-working session item surfaced beside the graph, the working-memory lane."""
    return st.builds(SessionNote, text=short_text, kind=short_text, score=scores)


def route_plans() -> st.SearchStrategy[RoutePlan]:
    """One router decision over the three routes and their lane toggles."""
    return st.builds(
        RoutePlan,
        route=st.sampled_from(list(QueryRoute)),
        ppr=st.booleans(),
        communities=st.booleans(),
        raptor=st.booleans(),
    )


def recall_results() -> st.SearchStrategy[RecallResult]:
    """One fused recall bundle, every lane populated independently."""
    return st.builds(
        RecallResult,
        query=short_text,
        hits=st.lists(hits(), max_size=5),
        facts=st.lists(fact_hits(), max_size=5),
        communities=st.lists(community_notes(), max_size=3),
        raptor=st.lists(raptor_notes(), max_size=3),
        session=st.lists(session_notes(), max_size=3),
        profile=st.none() | short_text,
        as_of=st.none() | aware_datetimes,
    )


def extracted_entities() -> st.SearchStrategy[ExtractedEntity]:
    """One extractor-proposed entity over the closed entity vocabulary."""
    return st.builds(ExtractedEntity, name=short_text, type=entity_types, attributes=st.just({}))


def llm_entities() -> st.SearchStrategy[WireEntity]:
    """One entity in the combined extraction call's compact wire schema (short keys n/t).

    Built against `ontology.current().llm_entity`, the live catalog's own dynamic class, so `t`
    only ever draws a name the current snapshot actually allows, requires `ops.setup()` to have
    already refreshed the cache, the same requirement `structured` itself has. `st.builds` only
    ever knows this as a plain `BaseModel`, its real shape is the live catalog's dynamic class,
    so the return is cast to the `WireEntity` protocol every caller actually reads.
    """
    snapshot = ontology.current()
    strategy = st.builds(
        snapshot.llm_entity, n=short_text, t=st.sampled_from(snapshot.entity_names)
    )
    return cast("st.SearchStrategy[WireEntity]", strategy)


def llm_facts() -> st.SearchStrategy[WireFact]:
    """One fact in the combined extraction wire schema (s/p/o + statement + optional date)."""
    snapshot = ontology.current()
    strategy = st.builds(
        snapshot.llm_fact,
        s=short_text,
        p=st.sampled_from(snapshot.relation_names),
        o=st.just("") | short_text,
        statement=short_text,
        date=st.none() | short_text,
    )
    return cast("st.SearchStrategy[WireFact]", strategy)


def llm_extractions() -> st.SearchStrategy[WireExtraction]:
    """One combined wire-schema extraction, entities and dated facts in one shot."""
    strategy = st.builds(
        ontology.current().llm_extraction,
        e=st.lists(llm_entities(), max_size=4),
        f=st.lists(llm_facts(), max_size=4),
    )
    return cast("st.SearchStrategy[WireExtraction]", strategy)


def extractions() -> st.SearchStrategy[Extraction]:
    """One combined node-and-edge slice from a span, its facts already dated."""
    return st.builds(
        Extraction,
        entities=st.lists(extracted_entities(), max_size=4),
        facts=st.lists(timed_facts(), max_size=4),
    )


def timed_facts() -> st.SearchStrategy[TimedFact]:
    """One dated structural fact, the candidate consolidation consumes."""
    return st.builds(
        TimedFact,
        subject=short_text,
        predicate=predicates,
        statement=short_text,
        valid_from=st.none() | aware_datetimes,
        valid_to=st.none() | aware_datetimes,
    )


def consolidation_verdicts() -> st.SearchStrategy[ConsolidationVerdict]:
    """One consolidation decision, a supersedes id present only on UPDATE."""
    return st.one_of(
        st.builds(ConsolidationVerdict, action=st.sampled_from(["ADD", "NOOP"])),
        st.builds(
            ConsolidationVerdict,
            action=st.just("UPDATE"),
            supersedes=st.none() | st.uuids(version=5),
        ),
    )


class TemporalState:
    """One Fact's temporal columns relative to a caller-chosen `now`, the bi-temporal probe.

    A property builds a `FactClaim` from these around its own `datetime.now(UTC)` and asserts the
    live gate, so the categories name the adversarial cases the gate must hide: a superseded
    version, a future-dated fact, and a closed-window fact.

    is_latest: whether `recorded`'s upper bound stays open, the live version of its statement.
    valid_from_days: day offset from now the `valid` window opens, negative past, None unbounded.
    valid_to_days: day offset from now the `valid` window closes, negative past, None unbounded.
    """

    def __init__(
        self, is_latest: bool, valid_from_days: float | None, valid_to_days: float | None
    ) -> None:
        self.is_latest = is_latest
        self.valid_from_days = valid_from_days
        self.valid_to_days = valid_to_days

    def window(self, now: datetime) -> tuple[datetime | None, datetime | None]:
        """The concrete valid_from and valid_to around `now`."""
        start = (
            None if self.valid_from_days is None else now + timedelta(days=self.valid_from_days)
        )
        end = None if self.valid_to_days is None else now + timedelta(days=self.valid_to_days)
        return start, end

    def valid(self, now: datetime) -> Range[datetime]:
        """The concrete `valid` range around `now`, unbounded on a side whose offset is None.

        The two offsets are drawn independently and may invert (a window closing before it opens),
        a case `expected_current` reads as never-current since no instant satisfies both bounds;
        Postgres's range type maps that same never-satisfiable window to `empty`.
        """
        start, end = self.window(now)
        if start is not None and end is not None and start > end:
            return Range(empty=True)
        return Range(start, end)

    def recorded(self, now: datetime) -> Range[datetime]:
        """The `recorded` range around `now`, open while `is_latest`, closed at `now` otherwise."""
        return Range(now - timedelta(days=1), None if self.is_latest else now)

    def expected_current(self, now: datetime) -> bool:
        """Whether the live gate must count this state current at `now`, the spec not the code."""
        start, end = self.window(now)
        return self.is_latest and (start is None or start <= now) and (end is None or end > now)


_offsets = st.none() | st.floats(
    min_value=-400.0, max_value=400.0, allow_nan=False, allow_infinity=False
).filter(lambda days: abs(days) > 1.0)


def temporal_states() -> st.SearchStrategy[TemporalState]:
    """A Fact temporal state spanning latest, superseded, future-dated, and closed-window cases."""
    return st.builds(
        TemporalState,
        is_latest=st.booleans(),
        valid_from_days=_offsets,
        valid_to_days=_offsets,
    )


@st.composite
def fact_timeline(draw: st.DrawFn, max_versions: int = 4) -> tuple[list[TemporalState], datetime]:
    """A chronological version history of one statement plus an as_of probe instant.

    Exactly one version is the latest, the rest superseded, the shape consolidation produces by
    closing the old version before inserting the new, so a bi-temporal property asserts that the
    live read surfaces at most the one latest and `as_of` replays the right version.
    """
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
    probe = draw(aware_datetimes)
    return states, probe
