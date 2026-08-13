from collections.abc import Mapping, Sequence

from patos import FrozenModel
from pydantic import UUID5, UUID7, Field, NonNegativeFloat, NonNegativeInt, PositiveInt

from .candidate import Candidate
from .lane import Lane


class RecallTraceRow(FrozenModel):
    """One candidate's position before and after merit ordering and packing."""

    statement_rank: PositiveInt
    merit_rank: PositiveInt
    score: float | None
    selected: bool
    lane: Lane.Kind
    line: str
    source_title: str | None = None


class RecallTiming(FrozenModel):
    """Wall time for each externally meaningful phase of one recall."""

    total_ms: NonNegativeFloat = 0.0
    embedding_ms: NonNegativeFloat = 0.0
    entity_detection_ms: NonNegativeFloat = 0.0
    database_ms: NonNegativeFloat = 0.0
    rerank_ms: NonNegativeFloat = 0.0
    packing_ms: NonNegativeFloat = 0.0
    access_recording_ms: NonNegativeFloat = 0.0
    statement_rows: NonNegativeInt = 0
    selected_rows: NonNegativeInt = 0
    statement_lanes: dict[str, NonNegativeInt] = Field(default_factory=dict)
    selected_lanes: dict[str, NonNegativeInt] = Field(default_factory=dict)


class RecallTrace(FrozenModel):
    """A diagnostic account of one recall without strengthening surfaced facts."""

    query: str
    budget: PositiveInt
    selected: NonNegativeInt
    rows: tuple[RecallTraceRow, ...]
    timing: RecallTiming = RecallTiming()

    @classmethod
    def build(
        cls,
        query: str,
        budget: int,
        statement: Sequence[Candidate],
        ranked: Sequence[Candidate],
        kept: Sequence[Candidate],
        scores: Mapping[UUID5 | UUID7 | None, float],
        timing: RecallTiming | None = None,
    ) -> RecallTrace:
        """Build the trace from the three explicit retrieval phases."""
        merit_positions = {id(candidate): rank for rank, candidate in enumerate(ranked, 1)}
        selected = cls.packed_identities(ranked, kept)
        return cls(
            query=query,
            budget=budget,
            selected=len(kept),
            timing=timing or RecallTiming(),
            rows=tuple(
                RecallTraceRow(
                    statement_rank=rank,
                    merit_rank=merit_positions[id(candidate)],
                    score=scores.get(candidate.evidence_id),
                    selected=id(candidate) in selected,
                    lane=candidate.lane,
                    line=candidate.line,
                    source_title=candidate.source_title,
                )
                for rank, candidate in enumerate(statement, 1)
            ),
        )

    @staticmethod
    def packed_identities(ranked: Sequence[Candidate], kept: Sequence[Candidate]) -> set[int]:
        """Which ranked candidates the pack returned, told apart by object identity.

        Identity is the only key that separates two rows carrying one evidence id, which
        overview claims of a single summary held in different scope sets genuinely do. Any
        key built from the values would mark every one of those rows selected the moment one
        of them was, leaving the row flags disagreeing with the selected count.

        Packing hands back the very objects it received, with one exception. An item trimmed
        to fit a budget too small to hold it whole is a fresh value that no ranked identity
        matches, and only the best-ranked item is ever trimmed, so a kept item nothing
        recognises belongs to the head of the ranking and nowhere else.
        """
        returned = {id(candidate) for candidate in kept}
        recognised = {id(candidate) for candidate in ranked if id(candidate) in returned}
        if len(recognised) < len(kept) and ranked:
            recognised.add(id(ranked[0]))
        return recognised

    def render(self) -> str:
        """Render a compact table followed by each full evidence line."""
        lines = [
            f"query  {self.query}",
            f"budget {self.budget}  selected {self.selected} of {len(self.rows)}",
            (
                f"timing {self.timing.total_ms:.1f} ms total  "
                f"embed {self.timing.embedding_ms:.1f}  "
                f"entities {self.timing.entity_detection_ms:.1f}  "
                f"database {self.timing.database_ms:.1f}  "
                f"rerank {self.timing.rerank_ms:.1f}  "
                f"pack {self.timing.packing_ms:.1f}  "
                f"access {self.timing.access_recording_ms:.1f}"
            ),
        ]
        for row in sorted(self.rows, key=lambda item: item.merit_rank):
            score = "unscored" if row.score is None else f"{row.score:.6f}"
            kept = "kept" if row.selected else "cut"
            source = f"  {row.source_title}" if row.source_title else ""
            lines.extend(
                (
                    f"{row.merit_rank:02d} <- {row.statement_rank:02d}  {score:>10}  "
                    f"{kept:4}  {row.lane}{source}",
                    f"    {row.line}",
                )
            )
        return "\n".join(lines)
