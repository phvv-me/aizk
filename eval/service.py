from collections.abc import Sequence
from pathlib import Path

from patos import FrozenModel
from pydantic import UUID5

from aizk.config import settings
from aizk.extract.extractor import Extractor
from aizk.ontology import Ontology
from aizk.retrieval import RecallTrace, trace
from aizk.store.identity import User

from .database import EvaluationDatabase
from .extraction import ExtractionBenchmark, ExtractionReport, load_extraction_cases
from .gate import GateReport, measure_gate
from .groupmem import GroupMemBench
from .management import ManagementBenchmark, ManagementReport
from .models import BenchmarkReport, QuestionKind
from .plans import PlanStudyReport, RetrievalBenchmark, Stratum
from .runner import BenchmarkRunner
from .scale import Budget, ScaleReport, run_scale_benchmark


class Evaluation(FrozenModel):
    """The cohesive evaluation facade used by the standalone CLI."""

    user_id: UUID5 | None = None

    @property
    def user(self) -> User:
        """Return the production corpus reader for this evaluation invocation."""
        return User.system({self.user_id or settings.system_user_id})

    async def production(
        self,
        k: int = 8,
        per_stratum: int = 8,
        strata: Sequence[str] = tuple(stratum.value for stratum in Stratum),
    ) -> PlanStudyReport:
        """Benchmark the production maximal plan over real stratified memory."""
        return await RetrievalBenchmark(
            user=self.user,
            k=k,
            per_stratum=per_stratum,
            strata=tuple(Stratum(stratum) for stratum in strata),
        ).production()

    async def trace(
        self,
        query: str,
        k: int = 8,
        token_budget: int = settings.context_token_budget,
    ) -> RecallTrace:
        """Explain one production recall without updating access history."""
        return await trace(query, self.user, k=k, token_budget=token_budget)

    async def management(
        self,
        kinds: Sequence[str] = ("area", "project"),
        k: int = 8,
        token_budget: int = settings.context_token_budget,
    ) -> ManagementReport:
        """Evaluate twenty grounded questions for every visible managed brief."""
        return await ManagementBenchmark(self.user, k=k, budget=token_budget).run(kinds)

    async def plans(
        self,
        k: int = 8,
        per_stratum: int = 8,
        strata: Sequence[str] = tuple(stratum.value for stratum in Stratum),
        seeding: bool = True,
        gate_limit: int | None = None,
    ) -> PlanStudyReport:
        """Run the stratified plan study and optional extraction gate replay."""
        report = await RetrievalBenchmark(
            user=self.user,
            k=k,
            per_stratum=per_stratum,
            strata=tuple(Stratum(stratum) for stratum in strata),
        ).diagnostic(seeding=seeding)
        if gate_limit is None:
            return report
        scopes = self.user.scopes.write
        return report.model_copy(update={"gate": await measure_gate(scopes, gate_limit)})

    async def gate(self, limit: int | None = 50) -> GateReport:
        """Replay the extraction gate over the selected live corpus."""
        return await measure_gate(self.user.scopes.write, limit)

    async def extraction(self, path: Path, model: str) -> ExtractionReport:
        """Reset the isolated database and score grounded graph extraction."""
        await EvaluationDatabase().reset()
        async with User.system() as session:
            await Ontology.ensure(session)
        return await ExtractionBenchmark(Extractor.configured()).run(
            load_extraction_cases(path), model
        )

    async def groupmem(
        self,
        root: Path,
        domain: str = "Finance",
        kinds: Sequence[str] = tuple(kind.value for kind in QuestionKind),
        message_limit: int | None = None,
        question_limit: int | None = None,
        k: int = 10,
        prepare: bool = True,
        keep: bool = False,
    ) -> BenchmarkReport:
        """Run GroupMemBench through the real write, recall, answer, and judge paths."""
        if prepare:
            await EvaluationDatabase().reset()
        dataset = GroupMemBench(root=root).load(
            domain,
            kinds=tuple(QuestionKind(kind) for kind in kinds),
            message_limit=message_limit,
            question_limit=question_limit,
        )
        return await BenchmarkRunner.configured(k=k).run(dataset, prepare=prepare, keep=keep)

    async def scale(
        self,
        sizes: Sequence[int] = (1_000, 10_000),
        k: int = 8,
        repeats: int = 10,
        recall_p95_ms: float = 200.0,
    ) -> ScaleReport:
        """Reset the isolated database and measure the synthetic scaling curve."""
        await EvaluationDatabase().reset()
        return await run_scale_benchmark(
            sizes=tuple(sizes),
            k=k,
            repeats=repeats,
            budget=Budget(recall_p95_ms=recall_p95_ms),
        )
