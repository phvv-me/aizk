import runpy
import sys
from pathlib import Path
from types import SimpleNamespace, TracebackType
from unittest.mock import AsyncMock, MagicMock

import dbutil
import pytest
from id_factory import uuid5
from patos import FrozenModel
from pydantic import UUID5

import eval.cli as cli_module
import eval.database as database_module
import eval.launcher as launcher_module
import eval.service as service_module
from aizk.retrieval import RecallTrace
from eval.cli import EvaluationCLI
from eval.database import EvaluationDatabase
from eval.gate import GateReport
from eval.management import ManagementReport
from eval.models import BenchmarkDataset, BenchmarkReport, QuestionKind
from eval.plans import PlanStudyReport, Stratum
from eval.scale import Budget, ScaleReport
from eval.service import Evaluation


class Rendered(FrozenModel):
    """Small report double for the command boundary."""

    text: str

    def render(self) -> str:
        return self.text


class RecordingEvaluation:
    """Validate CLI argument parsing without touching model services or PostgreSQL."""

    def __init__(self, user_id: UUID5 | None = None) -> None:
        self.user_id = user_id

    async def production(self, k: int, per_stratum: int, strata: list[str]) -> Rendered:
        assert (k, per_stratum, strata) == (3, 4, ["local", "global"])
        assert self.user_id is not None
        return Rendered(text="production")

    async def freeze(
        self,
        path: Path,
        per_stratum: int,
        strata: list[str],
    ) -> Rendered:
        assert (path, per_stratum, strata) == (
            Path("questions.jsonl"),
            2,
            ["local"],
        )
        return Rendered(text="freeze")

    async def trace(self, query: str, k: int, budget: int) -> Rendered:
        assert (query, k, budget) == ("why", 3, 500)
        return Rendered(text="trace")

    async def management(self, kinds: list[str], k: int, budget: int) -> Rendered:
        assert (kinds, k, budget) == (["area"], 3, 500)
        return Rendered(text="management")

    async def plans(
        self,
        k: int,
        per_stratum: int,
        strata: list[str],
        seeding: bool,
        gate_limit: int | None,
    ) -> Rendered:
        assert (k, per_stratum, strata, seeding, gate_limit) == (
            3,
            4,
            ["multihop"],
            False,
            5,
        )
        return Rendered(text="plans")

    async def gate(self, limit: int) -> Rendered:
        assert limit == 7
        return Rendered(text="gate")

    async def extraction(self, path: Path, model: str) -> Rendered:
        assert (path, model) == (Path("cases.jsonl"), "extractor")
        return Rendered(text="extraction")

    async def groupmem(
        self,
        root: Path,
        domain: str,
        kinds: list[str],
        message_limit: int | None,
        question_limit: int | None,
        k: int,
        prepare: bool,
        keep: bool,
    ) -> Rendered:
        assert (root, domain, kinds) == (Path("corpus"), "Lab", ["temporal"])
        assert (message_limit, question_limit, k, prepare, keep) == (2, 3, 4, False, True)
        return Rendered(text="groupmem")

    async def scale(
        self, sizes: tuple[int, ...], k: int, repeats: int, recall_p95_ms: float
    ) -> Rendered:
        assert (sizes, k, repeats, recall_p95_ms) == ((10, 20), 3, 4, 50.0)
        return Rendered(text="scale")


def test_cli_maps_strings_to_typed_evaluation_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli_module, "Evaluation", RecordingEvaluation)
    command = EvaluationCLI()
    user = uuid5()

    assert command.bench(3, 4, "local,global", user) == "production"
    assert command.freeze("questions.jsonl", 2, "local", user) == "freeze"
    assert command.trace("why", 3, 500, user) == "trace"
    assert command.management("area", 3, 500, user) == "management"
    assert command.plans(3, 4, "multihop", False, 5, user) == "plans"
    assert command.gate(7, user) == "gate"
    assert command.extraction("cases.jsonl", "extractor") == "extraction"
    assert command.groupmem("corpus", "Lab", "temporal", 2, 3, 4, False, True) == "groupmem"
    assert command.scale("10,20", 3, 4, 50.0) == "scale"

    output = tmp_path / "report.json"
    assert command.emit(Rendered(text="saved"), str(output)) == "saved"
    assert output.read_text(encoding="utf-8") == '{\n  "text": "saved"\n}'


def test_cli_main_gives_fire_the_command_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    fire = MagicMock()
    monkeypatch.setattr(cli_module.fire, "Fire", fire)

    cli_module.main()

    fire.assert_called_once_with(EvaluationCLI)


@pytest.mark.parametrize(
    ("arguments", "isolated"),
    [
        (["aizk-eval"], False),
        (["aizk-eval", "bench"], False),
        (["aizk-eval", "freeze"], False),
        (["aizk-eval", "scale"], True),
    ],
)
def test_launcher_isolates_only_destructive_commands(
    arguments: list[str], isolated: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = MagicMock()
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(launcher_module.os, "execvpe", execute)
    monkeypatch.setenv("AIZK_EVAL_DB_NAME", "custom_eval")
    monkeypatch.setenv("AIZK_DATABASE_URL", "production")
    monkeypatch.setenv("AIZK_ADMIN_DATABASE_URL", "production-admin")

    launcher_module.main()

    executable, command, environment = execute.call_args.args
    assert executable == sys.executable
    assert command == [sys.executable, "-m", "eval.cli", *arguments[1:]]
    assert environment["AIZK_EVAL_DB_NAME"] == "custom_eval"
    if isolated:
        assert environment["AIZK_DB_NAME"] == "custom_eval"
        assert "AIZK_DATABASE_URL" not in environment
        assert "AIZK_ADMIN_DATABASE_URL" not in environment
    else:
        assert environment["AIZK_DATABASE_URL"] == "production"
        assert environment["AIZK_ADMIN_DATABASE_URL"] == "production-admin"


def test_launcher_runs_as_a_module(monkeypatch: pytest.MonkeyPatch) -> None:
    execute = MagicMock()
    monkeypatch.setattr(sys, "argv", ["aizk-eval", "bench"])
    monkeypatch.setattr(launcher_module.os, "execvpe", execute)
    monkeypatch.delitem(sys.modules, "eval.launcher")

    runpy.run_module("eval.launcher", run_name="__main__")

    execute.assert_called_once()


def test_evaluation_database_refuses_live_names_and_resets_isolated_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = AsyncMock()
    monkeypatch.setattr(database_module.ops, "reset", reset)
    monkeypatch.setattr(database_module.settings, "db_name", "aizk")

    with pytest.raises(ValueError, match="ending in '_eval'"):
        dbutil.run(EvaluationDatabase().reset())

    monkeypatch.setattr(database_module.settings, "db_name", "aizk_eval")
    dbutil.run(EvaluationDatabase().reset())
    reset.assert_awaited_once_with()


def plan_report() -> PlanStudyReport:
    return PlanStudyReport(k=3, strata=[], seeding=None, routing=None)


def benchmark_report() -> BenchmarkReport:
    return BenchmarkReport(
        benchmark="GroupMemBench",
        domain="Lab",
        fingerprint="fingerprint",
        agent_model="agent",
        judge_model="judge",
        total=0,
        correct=0,
        failed=0,
        accuracy=0.0,
        by_kind={},
        complete_corpus=True,
        sampled_questions=False,
        solvability_filtered=False,
        reference_protocol=False,
        publishable=False,
        results=(),
    )


def test_evaluation_orchestrates_live_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    report = plan_report()
    management = ManagementReport(results=())
    gate = GateReport(
        chunks=0,
        accepted=0,
        rejected=0,
        rejected_with_facts=0,
        facts_lost=0,
        timed_out=0,
    )
    traced = RecallTrace(query="why", budget=500, selected=0, rows=())
    production = AsyncMock(return_value=report)
    diagnostic = AsyncMock(return_value=report)
    management_run = AsyncMock(return_value=management)
    trace = AsyncMock(return_value=traced)
    measure = AsyncMock(return_value=gate)
    frozen = MagicMock()
    freeze = AsyncMock(return_value=frozen)
    monkeypatch.setattr(service_module, "freeze_corpus", freeze)
    monkeypatch.setattr(service_module.RetrievalBenchmark, "production", production)
    monkeypatch.setattr(service_module.RetrievalBenchmark, "diagnostic", diagnostic)
    monkeypatch.setattr(service_module.ManagementBenchmark, "run", management_run)
    monkeypatch.setattr(service_module, "trace", trace)
    monkeypatch.setattr(service_module, "measure_gate", measure)
    owner = uuid5()
    evaluation = Evaluation(user_id=owner)

    async def body() -> None:
        assert evaluation.user.scopes.write == frozenset({owner})
        assert Evaluation().user.id == service_module.settings.system_user_id
        assert await evaluation.production(3, 4, ("local",)) is report
        assert await evaluation.freeze(Path("questions.jsonl"), 4, ("local",)) is frozen
        assert await evaluation.trace("why", 3, 500) is traced
        assert await evaluation.management(("area",), 3, 500) is management
        assert await evaluation.plans(3, 4, ("global",), False) is report
        with_gate = await evaluation.plans(3, 4, ("global",), False, 7)
        assert with_gate.gate is gate
        assert await evaluation.gate(8) is gate

    dbutil.run(body())
    production.assert_awaited_once_with()
    freeze.assert_awaited_once()
    assert freeze.await_args is not None
    assert freeze.await_args.args[0] == Path("questions.jsonl")
    assert freeze.await_args.args[2:] == (4, (Stratum.LOCAL,))
    diagnostic.assert_awaited()
    management_run.assert_awaited_once_with(("area",))
    trace.assert_awaited_once()
    assert measure.await_count == 2


class EvaluationSession:
    """Minimal async context for extraction setup."""

    def __init__(self) -> None:
        self.session = MagicMock()

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback


def test_evaluation_orchestrates_isolated_benchmarks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reset = AsyncMock()
    ensure = AsyncMock()
    extraction_report = MagicMock()
    extraction_run = AsyncMock(return_value=extraction_report)
    extractor = MagicMock()
    cases = MagicMock()
    session = EvaluationSession()
    monkeypatch.setattr(service_module.EvaluationDatabase, "reset", reset)
    monkeypatch.setattr(service_module.User, "system", staticmethod(lambda scopes=None: session))
    monkeypatch.setattr(service_module.Ontology, "ensure", ensure)
    monkeypatch.setattr(
        service_module.GraphClients,
        "from_settings",
        classmethod(lambda cls, config: SimpleNamespace(extractor=extractor)),
    )
    monkeypatch.setattr(service_module, "load_extraction_cases", lambda path: cases)
    extraction = MagicMock()
    extraction.return_value.run = extraction_run
    monkeypatch.setattr(service_module, "ExtractionBenchmark", extraction)

    dataset = BenchmarkDataset(
        name="sample",
        domain="Lab",
        fingerprint="fingerprint",
        messages=(),
        questions=(),
    )
    groupmem = MagicMock()
    groupmem.return_value.load.return_value = dataset
    monkeypatch.setattr(service_module, "GroupMemBench", groupmem)
    benchmark = benchmark_report()
    runner = MagicMock()
    runner.configured.return_value.run = AsyncMock(return_value=benchmark)
    monkeypatch.setattr(service_module, "BenchmarkRunner", runner)
    scale_report = ScaleReport(sizes=[], points=[], budget=Budget(recall_p95_ms=50), knees=[])
    scale = AsyncMock(return_value=scale_report)
    monkeypatch.setattr(service_module, "run_scale_benchmark", scale)
    path = tmp_path / "cases.jsonl"
    root = tmp_path / "groupmem"
    evaluation = Evaluation()

    async def body() -> None:
        assert await evaluation.extraction(path, "model") is extraction_report
        assert (
            await evaluation.groupmem(
                root,
                "Lab",
                (QuestionKind.temporal.value,),
                2,
                3,
                4,
                True,
                True,
            )
            is benchmark
        )
        assert await evaluation.groupmem(root, prepare=False) is benchmark
        assert await evaluation.scale((10, 20), 3, 4, 50) is scale_report

    dbutil.run(body())
    assert reset.await_count == 3
    ensure.assert_awaited_once_with(session.session)
    extraction.assert_called_once_with(extractor)
    extraction_run.assert_awaited_once_with(cases, "model")
    assert groupmem.call_args.kwargs == {"root": root}
    runner.configured.assert_called_with(k=10)
    assert runner.configured.return_value.run.await_count == 2
    scale.assert_awaited_once_with(sizes=(10, 20), k=3, repeats=4, budget=Budget(recall_p95_ms=50))
