import asyncio
from pathlib import Path
from typing import Literal, Protocol

import fire
from pydantic import UUID5

from aizk.config import settings

from .corpus import DEFAULT_PER_STRATUM, FROZEN_CORPUS_PATH
from .service import Evaluation


class Report(Protocol):
    """A renderable Pydantic evaluation report."""

    def render(self) -> str: ...

    def model_dump_json(self, *, indent: int | None = None) -> str: ...


class EvaluationCLI:
    """Standalone commands for live diagnostics and isolated benchmarks."""

    @staticmethod
    def emit(report: Report, out: str | None = None) -> str:
        """Return one rendered report and optionally persist its structured JSON."""
        if out:
            Path(out).write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return report.render()

    def bench(
        self,
        k: int = 8,
        per_stratum: int = 8,
        strata: str = "local,global,multihop",
        user: UUID5 | None = None,
        out: str | None = None,
    ) -> str:
        """Benchmark production retrieval over the configured live database."""
        report = asyncio.run(
            Evaluation(user_id=user).production(k, per_stratum, strata.split(","))
        )
        return self.emit(report, out)

    def freeze(
        self,
        path: str = str(FROZEN_CORPUS_PATH),
        per_stratum: int = DEFAULT_PER_STRATUM,
        strata: str = "local,global,multihop",
        user: UUID5 | None = None,
        out: str | None = None,
    ) -> str:
        """Generate and fingerprint the committed retrieval benchmark corpus."""
        report = asyncio.run(
            Evaluation(user_id=user).freeze(
                Path(path),
                per_stratum,
                strata.split(","),
            )
        )
        return self.emit(report, out)

    def trace(
        self,
        query: str,
        k: int = 8,
        budget: int = settings.context_token_budget,
        user: UUID5 | None = None,
        out: str | None = None,
    ) -> str:
        """Show statement rank, cross-encoder merit, and packing for one recall."""
        return self.emit(asyncio.run(Evaluation(user_id=user).trace(query, k, budget)), out)

    def management(
        self,
        kinds: str = "area,project",
        k: int = 8,
        budget: int = settings.context_token_budget,
        user: UUID5 | None = None,
        out: str | None = None,
    ) -> str:
        """Run twenty retrieval probes for every visible Area and Project brief."""
        report = asyncio.run(Evaluation(user_id=user).management(kinds.split(","), k, budget))
        return self.emit(report, out)

    def plans(
        self,
        k: int = 8,
        per_stratum: int = 8,
        strata: str = "local,global,multihop",
        seeding: bool = True,
        gate_limit: int | None = None,
        user: UUID5 | None = None,
        out: str | None = None,
    ) -> str:
        """Compare retired retrieval plans against the production plan."""
        report = asyncio.run(
            Evaluation(user_id=user).plans(k, per_stratum, strata.split(","), seeding, gate_limit)
        )
        return self.emit(report, out)

    def gate(
        self,
        limit: int = 50,
        user: UUID5 | None = None,
        out: str | None = None,
    ) -> str:
        """Replay the extraction gate on stored chunks."""
        return self.emit(asyncio.run(Evaluation(user_id=user).gate(limit)), out)

    def extraction(
        self,
        path: str,
        model: str = settings.llm_model,
        backend: Literal["llm", "gliner"] = settings.extract_backend,
        concurrency: int = 1,
        backlog: int = 10_704,
        out: str | None = None,
    ) -> str:
        """Score one explicit graph backend in the dedicated evaluation database."""
        return self.emit(
            asyncio.run(
                Evaluation().extraction(
                    Path(path),
                    model,
                    backend,
                    concurrency,
                    backlog,
                )
            ),
            out,
        )

    def groupmem(
        self,
        root: str,
        domain: str = "Finance",
        kinds: str = "multi_hop,knowledge_update,temporal,user_implicit,term_ambiguity,abstention",
        message_limit: int | None = None,
        question_limit: int | None = None,
        k: int = 10,
        prepare: bool = True,
        keep: bool = False,
        out: str | None = None,
    ) -> str:
        """Run GroupMemBench in the dedicated evaluation database."""
        report = asyncio.run(
            Evaluation().groupmem(
                Path(root),
                domain,
                kinds.split(","),
                message_limit,
                question_limit,
                k,
                prepare,
                keep,
            )
        )
        return self.emit(report, out)

    def scale(
        self,
        sizes: str = "1000,10000",
        k: int = 8,
        repeats: int = 10,
        recall_p95_ms: float = 200.0,
        out: str | None = None,
    ) -> str:
        """Measure the scaling curve in the dedicated evaluation database."""
        report = asyncio.run(
            Evaluation().scale(
                tuple(int(size) for size in sizes.split(",")),
                k,
                repeats,
                recall_p95_ms,
            )
        )
        return self.emit(report, out)


def main() -> None:
    """Run the standalone evaluation command tree."""
    fire.Fire(EvaluationCLI)


if __name__ == "__main__":
    main()
