from collections.abc import Mapping, Sequence

from patos import FrozenModel
from pydantic import UUID5, UUID7, NonNegativeInt, PositiveInt

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


class RecallTrace(FrozenModel):
    """A diagnostic account of one recall without strengthening surfaced facts."""

    query: str
    budget: PositiveInt
    selected: NonNegativeInt
    rows: tuple[RecallTraceRow, ...]

    @classmethod
    def build(
        cls,
        query: str,
        budget: int,
        statement: Sequence[Candidate],
        ranked: Sequence[Candidate],
        kept: Sequence[Candidate],
        scores: Mapping[UUID5 | UUID7 | None, float],
    ) -> RecallTrace:
        """Build the trace from the three explicit retrieval phases."""
        merit_positions = {id(candidate): rank for rank, candidate in enumerate(ranked, 1)}
        selected = {id(candidate) for candidate in kept}
        return cls(
            query=query,
            budget=budget,
            selected=len(kept),
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

    def render(self) -> str:
        """Render a compact table followed by each full evidence line."""
        lines = [
            f"query  {self.query}",
            f"budget {self.budget}  selected {self.selected} of {len(self.rows)}",
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
