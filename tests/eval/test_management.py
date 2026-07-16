from collections.abc import Sequence

import dbutil
import pytest
from id_factory import uuid5, uuid7, uuid8

import eval.management as management_module
from aizk.retrieval import Candidate, Lane
from aizk.store import Document
from aizk.store.identity import User
from eval.management import (
    ManagementBenchmark,
    ManagementProbe,
    ManagementQuestions,
    ManagementReport,
    ManagementResult,
    ManagementSubject,
)


def test_management_questions_render_twenty_distinct_grounded_probes_per_kind() -> None:
    areas = ManagementQuestions(name="Health", kind="area").questions
    projects = ManagementQuestions(name="Aizk", kind="project").questions

    assert len(areas) == len(set(areas)) == 20
    assert len(projects) == len(set(projects)) == 20
    assert all("Health" in question for question in areas)
    assert all("Aizk" in question for question in projects)


def test_management_report_summarizes_hits_ranks_status_and_an_empty_corpus() -> None:
    result = ManagementResult(
        subject=ManagementSubject(
            name="Aizk",
            kind="project",
            status="active",
        ),
        probes=(
            ManagementProbe(rank=1, latency_ms=10),
            ManagementProbe(rank=None, latency_ms=30),
            ManagementProbe(rank=2, latency_ms=20),
        ),
    )
    missing_status = ManagementResult(
        subject=ManagementSubject(name="Health", kind="area"),
        probes=(ManagementProbe(rank=None, latency_ms=40),),
    )
    report = ManagementReport(results=(result, missing_status))

    assert (result.hits, result.firsts) == (2, 1)
    assert result.reciprocal_rank == 0.5
    assert (report.hits, report.firsts, report.questions) == (2, 1, 4)
    assert (report.latency(0.5), report.latency(0.95)) == (20, 40)
    rendered = report.render()
    assert "active      2/ 3   1/ 3  0.500     20.0  Aizk" in rendered
    assert "area     -" in rendered
    assert "overall  2/4 hit 0.500  1/4 first 0.250  p50 20.0 ms  p95 40.0 ms" in rendered
    assert (
        ManagementReport(results=())
        .render()
        .endswith("overall  0/0 hit 0.000  0/0 first 0.000  p50 0.0 ms  p95 0.0 ms")
    )


@pytest.mark.usefixtures("migrated_db")
def test_management_benchmark_reads_only_named_managed_documents() -> None:
    owner = uuid5()
    user = User.private(owner)

    async def body() -> tuple[tuple[ManagementSubject, ...], tuple[ManagementSubject, ...]]:
        await dbutil.reset_db()
        async with user as session:
            session.add_all(
                (
                    Document(
                        id=uuid7(),
                        title="Health",
                        subject_type="area",
                        content_hash=uuid8(),
                        created_by=owner,
                        scopes=[owner],
                    ),
                    Document(
                        id=uuid7(),
                        title="Aizk",
                        subject_type="project",
                        content_hash=uuid8(),
                        created_by=owner,
                        scopes=[owner],
                    ),
                    Document(
                        id=uuid7(),
                        title="Ordinary note",
                        content_hash=uuid8(),
                        created_by=owner,
                        scopes=[owner],
                    ),
                    Document(
                        id=uuid7(),
                        subject_type="project",
                        content_hash=uuid8(),
                        created_by=owner,
                        scopes=[owner],
                    ),
                )
            )
        benchmark = ManagementBenchmark(user)
        return await benchmark.subjects(("area", "project", "note")), await benchmark.subjects(
            ("note",)
        )

    subjects, empty = dbutil.run(body())
    assert subjects == (
        ManagementSubject(name="Health", kind="area"),
        ManagementSubject(name="Aizk", kind="project"),
    )
    assert empty == ()


def test_management_benchmark_finds_source_rank_and_preserves_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid5()
    user = User.authorized(owner, read=(owner,), write=(owner,), label="Pedro")
    calls = 0

    async def recall(
        question: str,
        caller: User,
        k: int,
        token_budget: int,
    ) -> list[Candidate]:
        nonlocal calls
        calls += 1
        assert question == "question"
        assert caller is not user
        assert caller.id == user.id and caller.scopes == user.scopes
        assert (k, token_budget) == (4, 500)
        return [
            Candidate(lane=Lane.Kind.FACTS, line="other", source_title="Other"),
            Candidate(lane=Lane.Kind.SOURCES, line="brief", source_title="Aizk"),
        ]

    monkeypatch.setattr(management_module, "recall", recall)
    benchmark = ManagementBenchmark(user, k=4, budget=500)

    assert dbutil.run(benchmark.probe("question", "Aizk")).rank == 2
    assert dbutil.run(benchmark.probe("question", "Missing")).rank is None
    assert calls == 2


def test_management_benchmark_runs_twenty_questions_for_every_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subjects = (
        ManagementSubject(name="Health", kind="area"),
        ManagementSubject(name="Aizk", kind="project", status="active"),
    )
    questions: list[tuple[str, str]] = []

    async def selected(
        self: ManagementBenchmark, kinds: Sequence[str]
    ) -> tuple[ManagementSubject, ...]:
        assert tuple(kinds) == ("area", "project")
        return subjects

    async def probe(self: ManagementBenchmark, question: str, subject: str) -> ManagementProbe:
        questions.append((question, subject))
        return ManagementProbe(rank=1, latency_ms=10)

    monkeypatch.setattr(ManagementBenchmark, "subjects", selected)
    monkeypatch.setattr(ManagementBenchmark, "probe", probe)

    report = dbutil.run(ManagementBenchmark(User.system(), concurrency=2).run())

    assert len(questions) == 40
    assert report.hits == report.questions == 40
    assert report.firsts == report.questions
    assert all(result.reciprocal_rank == 1.0 for result in report.results)
