import uuid
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import dbutil
import pytest
import seedgraph
from sqlalchemy import text

import aizk.admin as admin
from aizk.config import settings
from aizk.eval import Budget, QuestionKind
from aizk.extract import ontology
from aizk.store import as_system, engine
from aizk.store.identity import User

DOC_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DOC_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SYSTEM = settings.system_user_id
SENTINEL = object()


class Recorder:
    def __init__(self, ret: object = None) -> None:
        self.ret = ret
        self.args: tuple[object, ...] = ()
        self.kwargs: dict[str, object] = {}

    async def __call__(self, *args: object, **kwargs: object) -> object:
        self.args = args
        self.kwargs = kwargs
        return self.ret


class SyncRecorder:
    def __init__(self, ret: object = None) -> None:
        self.ret = ret
        self.args: tuple[object, ...] = ()

    def __call__(self, *args: object) -> object:
        self.args = args
        return self.ret


def test_system_is_the_configured_system_user() -> None:
    assert admin.system() == settings.system_user_id


@pytest.mark.parametrize(
    ("fn", "delegate", "ret", "expected"),
    [
        ("rebuild", "build_graph", (3, 5), (3, 5)),
        ("decay", "decay", 7, 7),
        ("reembed", "reembed", 9, 9),
        ("raptor", "build_raptor", 4, 4),
    ],
)
@pytest.mark.parametrize("user_id", [None, DOC_A], ids=["default", "explicit"])
def test_maintenance_op_defaults_to_the_system_user(
    monkeypatch: pytest.MonkeyPatch,
    fn: str,
    delegate: str,
    ret: object,
    expected: object,
    user_id: uuid.UUID | None,
) -> None:
    recorder = Recorder(ret=ret)
    monkeypatch.setattr(admin.graph, delegate, recorder)

    out = dbutil.run(getattr(admin, fn)(user_id=user_id))

    assert out == expected
    assert recorder.kwargs["scopes"] == frozenset({user_id or settings.system_user_id})


def test_forget_ranks_documents_by_the_query_then_retracts_their_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_embed(texts: list[str], mode: str) -> list[list[float]]:
        return [[0.1, 0.2]]

    class FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        async def exec(self, statement: object) -> list[object]:
            self.calls += 1
            return [DOC_A, DOC_B] if self.calls == 1 else ["Note A", None]

    @asynccontextmanager
    async def fake_transaction(user: User):
        yield FakeSession()

    async def fake_forget(session: object, doc_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return doc_ids  # every named document contributed one live claim

    monkeypatch.setattr(admin, "embed", fake_embed)
    monkeypatch.setattr(engine, "transaction", fake_transaction)
    monkeypatch.setattr(admin.FactClaim, "forget_from_documents", fake_forget)

    result = dbutil.run(admin.forget("a wrong note", k=8))

    assert result.claims == 2  # both ranked documents' claims retracted
    assert result.documents == ["Note A"]  # the null title dropped, the real one kept


def test_audit_lists_the_recent_visible_writes(migrated_db: None) -> None:
    async def run() -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        await dbutil.reset_db()
        first = await dbutil.seed_document(SYSTEM, [SYSTEM])
        second = await dbutil.seed_document(SYSTEM, [SYSTEM])
        docs = await admin.audit(limit=10)
        return {doc.id for doc in docs}, {first, second}

    seen, expected = dbutil.run(run())
    assert seen == expected


@dataclass
class Seam:
    id: str
    owner: object
    attr: str
    ret: object
    call: Callable[[], Coroutine[object, object, object]]
    args: tuple[object, ...]
    kwargs: dict[str, object]


SEAMS = [
    Seam(
        "export_scope",
        admin.export,
        "export_scope",
        SENTINEL,
        lambda: admin.export_scope("dump.jsonl"),
        (Path("dump.jsonl"),),
        {"user": admin.User.system({SYSTEM})},
    ),
    Seam("tasks_status", admin, "tasks_overview", SENTINEL, admin.tasks_status, (), {}),
    Seam("setup", admin.ops, "setup", SENTINEL, admin.setup, (), {}),
    Seam("health", admin.ops, "health", SENTINEL, admin.health, (), {}),
]


def test_promote_resolves_the_target_and_passes_complete_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = (admin.settings.scope_id(name) for name in ("team", "vault"))
    recorder = Recorder(ret=5)
    monkeypatch.setattr(admin.graph, "promote", recorder)

    assert dbutil.run(admin.promote(str(DOC_A), "team,vault")) == 5
    assert recorder.args[:2] == ([DOC_A], frozenset({first, second}))
    user = recorder.args[2]
    assert isinstance(user, admin.User)
    assert user.id == SYSTEM
    assert user.scopes.read == user.scopes.write == frozenset({SYSTEM, first, second})


@pytest.mark.parametrize("seam", SEAMS, ids=[s.id for s in SEAMS])
def test_operator_verb_forwards_argv_to_its_seam(
    monkeypatch: pytest.MonkeyPatch, seam: Seam
) -> None:
    recorder = Recorder(ret=seam.ret)
    monkeypatch.setattr(seam.owner, seam.attr, recorder)

    out = dbutil.run(seam.call())

    assert out == seam.ret
    assert recorder.args == seam.args
    assert recorder.kwargs == seam.kwargs


def test_groupmem_loads_the_named_kinds_then_runs_the_configured_benchmark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = object()
    loaded: dict[str, object] = {}
    ran: dict[str, object] = {}

    class FakeBench:
        def __init__(self, root: Path) -> None:
            loaded["root"] = root

        def load(
            self,
            domain: str,
            *,
            kinds: tuple[QuestionKind, ...],
            message_limit: int | None,
            question_limit: int | None,
        ) -> object:
            loaded["domain"], loaded["kinds"] = domain, kinds
            loaded["limits"] = (message_limit, question_limit)
            return dataset

    class FakeRunner:
        def __init__(self, k: int) -> None:
            self.k = k

        @classmethod
        def configured(cls, k: int) -> FakeRunner:
            ran["k"] = k
            return cls(k)

        async def run(self, ds: object, prepare: bool, keep: bool) -> str:
            ran["dataset"], ran["prepare"], ran["keep"] = ds, prepare, keep
            return "REPORT"

    monkeypatch.setattr(admin, "GroupMemBench", FakeBench)
    monkeypatch.setattr(admin, "BenchmarkRunner", FakeRunner)

    out = dbutil.run(
        admin.groupmem(
            "corpus/root",
            domain="Finance",
            kinds=("temporal", "abstention"),
            message_limit=2,
            question_limit=3,
            k=7,
            prepare=False,
            keep=True,
        )
    )

    assert out == "REPORT"
    assert loaded["root"] == Path("corpus/root")
    assert loaded["domain"] == "Finance"
    assert loaded["kinds"] == (QuestionKind.temporal, QuestionKind.abstention)
    assert loaded["limits"] == (2, 3)
    assert ran == {"k": 7, "dataset": dataset, "prepare": False, "keep": True}


def test_scale_forwards_sizes_and_wraps_the_recall_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(ret=SENTINEL)
    monkeypatch.setattr(admin, "run_scale_benchmark", recorder)

    out = dbutil.run(admin.scale(sizes=(1, 2, 3), k=3, repeats=4, recall_p95_ms=150.0))

    budget = recorder.kwargs["budget"]
    assert isinstance(budget, Budget)
    assert out is SENTINEL
    assert recorder.kwargs["sizes"] == (1, 2, 3)
    assert recorder.kwargs["k"] == 3 and recorder.kwargs["repeats"] == 4
    assert budget.recall_p95_ms == 150.0


def test_profile_report_reads_the_span_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = ["span-a", "span-b"]

    class Collector:
        def stats(self) -> list[str]:
            return stats

    monkeypatch.setattr(admin, "default_collector", lambda: Collector())

    assert admin.profile_report() is stats


def test_ingest_resolves_scopes_then_owns_the_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = admin.settings.scope_id("org_team")
    ingest = Recorder(ret=4)
    monkeypatch.setattr(admin.extract_ingest, "ingest_path", ingest)

    out = dbutil.run(admin.ingest("notes/dir", scopes="org_team"))

    assert out == 4
    assert ingest.args == (User.system(frozenset({scope})), Path("notes/dir"))
    assert ingest.kwargs == {"created_by": SYSTEM, "scopes": frozenset({scope})}


def test_ingest_image_resolves_scopes_then_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = admin.settings.scope_id("org_team")
    ingest = Recorder(ret=DOC_A)
    monkeypatch.setattr(admin.extract_ingest, "ingest_image", ingest)

    out = dbutil.run(admin.ingest_image("pic.png", caption="a cat", scopes="org_team"))

    assert out == DOC_A
    assert ingest.args == (User.system(frozenset({scope})), Path("pic.png"))
    assert ingest.kwargs == {
        "caption": "a cat",
        "created_by": SYSTEM,
        "scopes": frozenset({scope}),
    }


@pytest.mark.parametrize("with_file", [False, True], ids=["synthesized", "from-file"])
def test_bench_reads_questions_then_runs_the_eval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, with_file: bool
) -> None:
    recorder = Recorder(ret="REPORT")
    monkeypatch.setattr(admin, "run_eval", recorder)
    questions_file: str | None = None
    if with_file:
        path = tmp_path / "q.txt"
        path.write_text("first?\nsecond?\n", encoding="utf-8")
        questions_file = str(path)

    out = dbutil.run(admin.bench(questions_file=questions_file, k=5))

    assert out == "REPORT"
    assert recorder.args[0] == (["first?", "second?"] if with_file else None)
    assert recorder.kwargs == {"k": 5, "user": admin.User.system()}


def test_sweep_runs_only_axes_compatible_with_the_live_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(ret="SWEEP")
    monkeypatch.setattr(admin, "run_sweep", recorder)

    out = dbutil.run(admin.sweep(k=3))

    assert out == "SWEEP"
    assert recorder.args[0] is None  # no questions file, synthesized
    assert recorder.kwargs == {"k": 3, "user": admin.User.system()}


@pytest.mark.parametrize("gate_limit", [None, 5])
def test_plan_study_resolves_strata_and_optionally_replays_the_gate(
    monkeypatch: pytest.MonkeyPatch, gate_limit: int | None
) -> None:
    study = Recorder(ret=admin.PlanStudyReport(k=4, strata=[], seeding=None, routing=None))
    gate_report = admin.GateReport(
        chunks=1, accepted=1, rejected=0, rejected_with_facts=0, facts_lost=0, timed_out=0
    )
    gate = Recorder(ret=gate_report)
    monkeypatch.setattr(admin, "run_plan_study", study)
    monkeypatch.setattr(admin, "measure_gate", gate)

    report = dbutil.run(
        admin.plan_study(k=4, per_stratum=2, strata=("local", "multihop"), gate_limit=gate_limit)
    )

    assert study.kwargs["strata"] == (admin.Stratum.LOCAL, admin.Stratum.MULTIHOP)
    assert study.kwargs["k"] == 4 and study.kwargs["per_stratum"] == 2
    assert study.kwargs["user"] == admin.User.system()
    if gate_limit is None:
        assert gate.kwargs == {} and report.gate is None
    else:
        assert gate.kwargs == {"limit": 5}
        assert report.gate == gate_report


def test_gate_check_scopes_the_replay_to_the_acting_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(ret="GATE")
    monkeypatch.setattr(admin, "measure_gate", recorder)
    actor = uuid.uuid4()

    out = dbutil.run(admin.gate_check(limit=9, user_id=actor))

    assert out == "GATE"
    assert recorder.kwargs == {"scopes": frozenset({actor}), "limit": 9}


async def _catalog_row(sql: str, name: str) -> tuple[str, str] | None:
    async with dbutil.admin_engine().begin() as connection:
        row = (await connection.execute(text(sql), {"name": name})).first()
    return (row[0], row[1]) if row is not None else None


def test_define_entity_and_relation_kind_write_the_catalog(
    migrated_db: None, fake_embedder: object
) -> None:
    async def run() -> tuple[
        tuple[str, str] | None, tuple[str, str] | None, tuple[str, str] | None
    ]:
        await dbutil.reset_db()
        try:
            await admin.define_entity_kind("TestWidget", "a widget gloss", "coding")
            await admin.define_relation_kind("test_powers", "x powers y", "research")
            entity = await _catalog_row(
                "SELECT description, domain FROM entity_kind WHERE name = :name", "test_widget"
            )
            relation = await _catalog_row(
                "SELECT description, domain FROM relation_kind WHERE name = :name", "test_powers"
            )
            await admin.define_entity_kind("TestWidget", "a sharper gloss", "general")
            refined = await _catalog_row(
                "SELECT description, domain FROM entity_kind WHERE name = :name", "test_widget"
            )
            return entity, relation, refined
        finally:
            await dbutil.admin_exec("DELETE FROM entity_kind WHERE name = 'test_widget'")
            await dbutil.admin_exec("DELETE FROM relation_kind WHERE name = 'test_powers'")
            async with as_system() as session:
                await ontology.refresh(session)

    entity, relation, refined = dbutil.run(run())

    assert entity == ("a widget gloss", "coding")  # canonicalized name, verbatim gloss and domain
    assert relation == ("x powers y", "research")
    assert refined == ("a sharper gloss", "general")  # a repeat refines rather than duplicates


def test_list_ontology_reports_kinds_with_live_use_counts(migrated_db: None) -> None:
    async def run() -> list[admin.OntologyKindRow]:
        await dbutil.reset_db()
        async with dbutil.actor(SYSTEM) as session:
            subject = await seedgraph.add_entity(session, SYSTEM, "Widget", type="concept")
            await seedgraph.add_fact(session, SYSTEM, subject, "widget relates to gadget")
        return await admin.list_ontology()

    rows = dbutil.run(run())

    indexed = {(row.kind, row.name): row for row in rows}
    assert indexed[("entity", "concept")].uses >= 1
    assert indexed[("relation", "related_to")].uses >= 1
    assert indexed[("entity", "raptor_summary")].structural is True  # structural flag surfaced
    kinds = [row.kind for row in rows]
    assert kinds == sorted(
        kinds, key=lambda kind: kind != "entity"
    )  # all entities, then relations
