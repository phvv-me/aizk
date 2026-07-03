import uuid
from datetime import UTC, datetime, timedelta

from factories import AizkTableFactory
from hypothesis import strategies as st
from polyfactory import Use
from sqlalchemy.dialects.postgresql import Range

from aizk.config import Settings
from aizk.extract.models import (
    ConsolidationVerdict,
    ExtractedEntity,
    ExtractedFact,
    Extraction,
    FactTimestamp,
    TimedFact,
    TimestampResolution,
)
from aizk.extract.ontology import EntityType, RelationType
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
from aizk.store import Community, Document, EntityClaim, EntityContent, Profile, Watermark

# the extractable ontology vocabularies, already sorted for a byte-stable draw, the same
# EntityType/RelationType members the extraction prompt and pydantic fields share
ENTITY_TYPE_LIST: list[EntityType] = EntityType.extractable()
PREDICATE_LIST: list[RelationType] = RelationType.extractable()

# strategies over the two extractable vocabularies, the only entity types and predicates the
# ORM validators and the extractor fields admit from the extractor itself
entity_types = st.sampled_from(ENTITY_TYPE_LIST)
predicates = st.sampled_from(PREDICATE_LIST)

# short non-empty surface text for names, statements, and snippets, kept printable so a failing
# example reads cleanly
short_text = st.text(min_size=1, max_size=40)

# finite scores, the fused relevance values every result model carries
scores = st.floats(allow_nan=False, allow_infinity=False, allow_subnormal=False)

# timezone-aware instants, the only datetimes the bi-temporal columns store
aware_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(UTC),
)


def vector(width: int = Settings().embed_dim) -> st.SearchStrategy[list[float]]:
    """A single embedding row of exactly `width` finite floats.

    width: required vector width, the halfvec dimension by default.
    """
    return st.lists(
        st.floats(allow_nan=False, allow_infinity=False, width=16),
        min_size=width,
        max_size=width,
    )


def vectors(width: int = Settings().embed_dim) -> st.SearchStrategy[list[list[float]]]:
    """A batch of correctly-sized embedding rows, the happy input `validated` returns unchanged.

    width: width every row in the batch carries.
    """
    return st.lists(vector(width), min_size=0, max_size=4)


# result-model strategies, each derived from the model's own field types so a schema change forces
# the strategy to follow rather than drift


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


def extracted_facts() -> st.SearchStrategy[ExtractedFact]:
    """One structural triple over the closed predicate vocabulary, object optional."""
    return st.builds(
        ExtractedFact,
        subject=short_text,
        predicate=predicates,
        statement=short_text,
    )


def extractions() -> st.SearchStrategy[Extraction]:
    """One combined node-and-edge slice from a span."""
    return st.builds(
        Extraction,
        entities=st.lists(extracted_entities(), max_size=4),
        facts=st.lists(extracted_facts(), max_size=4),
    )


def fact_timestamps() -> st.SearchStrategy[FactTimestamp]:
    """One resolved valid-time window, either bound optionally absent."""
    return st.builds(
        FactTimestamp,
        valid_from=st.none() | aware_datetimes,
        valid_to=st.none() | aware_datetimes,
    )


def timestamp_resolutions(n: int) -> st.SearchStrategy[TimestampResolution]:
    """A resolution carrying exactly `n` windows, the aligned-by-position pass output.

    n: number of windows the pass returns, one per fact handed to it.
    """
    return st.builds(
        TimestampResolution,
        timestamps=st.lists(fact_timestamps(), min_size=n, max_size=n),
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

    A property builds a `Fact` from these around its own `datetime.now(UTC)` and asserts the live
    gate, so the categories below name exactly the adversarial cases the gate must hide: a
    superseded version, a future-dated fact, and a closed-window fact.

    is_latest: whether `recorded`'s upper bound stays open, the live version of its statement.
    valid_from_days: offset in days from now the `valid` window opens, negative is past, None
        opens it.
    valid_to_days: offset in days from now the `valid` window closes, negative is past, None
        leaves it.
    """

    def __init__(
        self, is_latest: bool, valid_from_days: float | None, valid_to_days: float | None
    ) -> None:
        self.is_latest = is_latest
        self.valid_from_days = valid_from_days
        self.valid_to_days = valid_to_days

    def window(self, now: datetime) -> tuple[datetime | None, datetime | None]:
        """The concrete valid_from and valid_to around `now`.

        now: the reference instant the offsets are measured from.
        """
        start = (
            None if self.valid_from_days is None else now + timedelta(days=self.valid_from_days)
        )
        end = None if self.valid_to_days is None else now + timedelta(days=self.valid_to_days)
        return start, end

    def valid(self, now: datetime) -> Range[datetime]:
        """The concrete `valid` range around `now`, unbounded on a side whose offset is None.

        The two offsets are drawn independently and may invert (a window that closes before it
        opens), a case `expected_current` already reads as never-current since no instant can
        satisfy both bounds at once. Postgres's range type has no such inverted state, so the same
        never-satisfiable window maps to the range type's own equivalent, `empty`.

        now: the reference instant the offsets are measured from.
        """
        start, end = self.window(now)
        if start is not None and end is not None and start > end:
            return Range(empty=True)
        return Range(start, end)

    def recorded(self, now: datetime) -> Range[datetime]:
        """The concrete `recorded` range around `now`, open while `is_latest`, closed at `now`
        otherwise, the version-history shape consolidation produces.

        now: the reference instant the state is recorded around.
        """
        return Range(now - timedelta(days=1), None if self.is_latest else now)

    def expected_current(self, now: datetime) -> bool:
        """Whether the live gate must count this state as current at `now`, the spec, not the code.

        now: the reference instant the gate is evaluated at.
        """
        start, end = self.window(now)
        return self.is_latest and (start is None or start <= now) and (end is None or end > now)


# day offsets that straddle now on both sides, so a window can open or close in the past or future
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

    Exactly one version is the latest, the rest are superseded, the shape consolidation produces by
    closing the old version before inserting the new one, so a bi-temporal property can assert that
    `visible_at(None)` surfaces at most the one latest and `as_of` replays the right version.

    max_versions: ceiling on how many superseded-plus-latest versions the history holds.
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


@st.composite
def scope_principals(draw: st.DrawFn) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Three distinct ids, two principals and a bridging group, the multi-principal RLS lattice.

    The shape the no-leak probe seeds, principal A, principal B, and a group only one of them
    joins, so the only sharing path between A and B is that group's scope.
    """
    ids = draw(st.lists(st.uuids(version=4), min_size=3, max_size=3, unique=True))
    return ids[0], ids[1], ids[2]


# polyfactory ORM factories over the scoped models, for the skippable DB tests that need a real
# row. The validated columns are pinned to the closed vocabulary so a built row passes the ORM
# boundary, and the embedding stays null through the shared halfvec mapping in the base factory.


class EntityContentFactory(AizkTableFactory[EntityContent]):
    """Builds a transient `EntityContent`, its type pinned to a sampled ontology type."""

    type = Use(ENTITY_TYPE_LIST.__getitem__, 0)


class EntityClaimFactory(AizkTableFactory[EntityClaim]):
    """Builds a transient `EntityClaim`, one container's stake in an entity content row."""


class DocumentFactory(AizkTableFactory[Document]):
    """Builds a transient `Document`, the parent of its chunks."""


class CommunityFactory(AizkTableFactory[Community]):
    """Builds a transient `Community`, one summarized entity cluster."""


class ProfileFactory(AizkTableFactory[Profile]):
    """Builds a transient `Profile`, one entity's rolled-up portrait."""


class WatermarkFactory(AizkTableFactory[Watermark]):
    """Builds a transient `Watermark`, the per-principal bookkeeping counter."""
