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
from aizk.eval import Budget, SweepMatrix
from aizk.extract import ontology
from aizk.store import acting_as, as_system

DOC_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DOC_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SYSTEM = settings.system_user_id
SENTINEL = object()


class Recorder:
    """An async call double recording its arguments and resolving to a fixed value."""

    def __init__(self, ret: object = None) -> None:
        self.ret = ret
        self.args: tuple[object, ...] = ()
        self.kwargs: dict[str, object] = {}

    async def __call__(self, *args: object, **kwargs: object) -> object:
        self.args = args
        self.kwargs = kwargs
        return self.ret


class SyncRecorder:
    """A synchronous call double, for a plain function seam like `scopes_from_org_ids`."""

    def __init__(self, ret: object = None) -> None:
        self.ret = ret
        self.args: tuple[object, ...] = ()

    def __call__(self, *args: object) -> object:
        self.args = args
        return self.ret


def test_system_is_the_configured_system_user() -> None:
    """An operator call acts as the system user by default, past row level security."""
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
def test_maintenance_op_defaults_to_the_system_user(
    monkeypatch: pytest.MonkeyPatch, fn: str, delegate: str, ret: object, expected: object
) -> None:
    """A maintenance op with no explicit user drives its graph delegate as the system one."""
    recorder = Recorder(ret=ret)
    monkeypatch.setattr(admin.graph, delegate, recorder)

    out = dbutil.run(getattr(admin, fn)())

    assert out == expected
    assert recorder.kwargs["user_id"] == settings.system_user_id


def test_maintenance_op_honors_an_explicit_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named user overrides the system default, the scoped-view seam for a tenant op."""
    recorder = Recorder(ret=0)
    monkeypatch.setattr(admin.graph, "decay", recorder)
    who = uuid.uuid4()

    dbutil.run(admin.decay(half_life_days=30.0, user_id=who))

    assert recorder.kwargs["user_id"] == who
    assert recorder.kwargs["half_life_days"] == 30.0


def test_forget_ranks_documents_by_the_query_then_retracts_their_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forget embeds the query, ranks the nearest notes, and retracts their derived claims.

    The provenance chain the operator's erasure runs: one embed, a nearest-document rank under the
    user's own RLS, and a `forget_from_documents` over exactly those ids, the titles reported
    back so the operator sees what left before committing.
    """

    class FakeEmbedder:
        async def embed(self, texts: list[str], mode: str) -> list[list[float]]:
            return [[0.1, 0.2]]

    class Result:
        def __init__(self, values: list[object]) -> None:
            self._values = values

        def scalars(self) -> list[object]:
            return self._values

    class FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, statement: object) -> Result:
            self.calls += 1
            # first execute ranks document ids, second reads their titles
            return Result([DOC_A, DOC_B]) if self.calls == 1 else Result(["Note A", None])

    @asynccontextmanager
    async def fake_acting_as(user_id: uuid.UUID):
        fake = FakeSession()
        async with dbutil.use_session(fake):
            yield fake

    async def fake_forget(doc_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return doc_ids  # every named document contributed one live claim

    monkeypatch.setattr(admin, "Embedder", FakeEmbedder)
    monkeypatch.setattr(admin, "acting_as", fake_acting_as)
    monkeypatch.setattr(admin.FactClaim, "forget_from_documents", fake_forget)

    result = dbutil.run(admin.forget("a wrong note", k=8))

    assert result.claims == 2  # both ranked documents' claims retracted
    assert result.documents == ["Note A"]  # the null title dropped, the real one kept


def test_benchmark_refuses_when_the_engine_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The benchmark op is gated off by default, an explicit opt-in the datasets need."""
    monkeypatch.setattr(settings, "benchmarks_enabled", False)
    with pytest.raises(ValueError, match="benchmarks are off"):
        dbutil.run(admin.benchmark("evermembench", "x.jsonl"))


def test_benchmark_rejects_an_unknown_dataset_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the engine on, an unknown benchmark name is a fail-fast, not a silent empty sweep."""
    monkeypatch.setattr(settings, "benchmarks_enabled", True)
    with pytest.raises(ValueError, match="unknown benchmark"):
        dbutil.run(admin.benchmark("nope", "x.jsonl"))


def test_audit_lists_the_recent_visible_writes(migrated_db: None) -> None:
    """`audit` reads the operator's most recent visible document writes under its own RLS.

    Audit is inline now rather than a `UserRow.recent_writes` delegate, so it is driven against the
    live schema: two system-owned private documents are seeded and both read back through the
    system user's own visibility.
    """

    async def run() -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        await dbutil.reset_db()
        first = await dbutil.seed_document(SYSTEM, [])
        second = await dbutil.seed_document(SYSTEM, [])
        docs = await admin.audit(limit=10)
        return {doc.id for doc in docs}, {first, second}

    seen, expected = dbutil.run(run())
    assert seen == expected


@dataclass
class Seam:
    """One operator verb that forwards to a single external seam, defaulting the actor to system.

    id: parametrize label for the verb.
    owner: object holding the seam attribute a recorder replaces.
    attr: name of the seam attribute on `owner`.
    ret: value the seam resolves to, handed back unchanged by the verb.
    call: builds the admin coroutine to drive once the recorder is installed.
    args: the positional arguments the verb must forward to the seam.
    kwargs: the keyword arguments the verb must forward to the seam.
    """

    id: str
    owner: object
    attr: str
    ret: object
    call: Callable[[], Coroutine[object, object, object]]
    args: tuple[object, ...]
    kwargs: dict[str, object]


# each verb forwards its parsed argv to exactly one seam and returns that seam's value, acting as
# the system user when none is named. The document id round-trips through `str` back to a `uuid`,
# the export path through `Path`, so the verb's own parsing rides along with the wiring assertion.
SEAMS = [
    Seam(
        "promote",
        admin.graph,
        "promote",
        5,
        lambda: admin.promote(str(DOC_A), "team,vault"),
        (DOC_A, "team,vault"),
        {"user_id": SYSTEM},
    ),
    Seam(
        "export_scope",
        admin.export,
        "export_scope",
        SENTINEL,
        lambda: admin.export_scope("dump.jsonl"),
        (Path("dump.jsonl"),),
        {"user_id": SYSTEM},
    ),
    Seam("tasks_status", admin, "tasks_overview", SENTINEL, admin.tasks_status, (), {}),
    Seam("setup", admin.ops, "setup", SENTINEL, admin.setup, (), {}),
    Seam("health", admin.ops, "health", SENTINEL, admin.health, (), {}),
]


@pytest.mark.parametrize("seam", SEAMS, ids=[s.id for s in SEAMS])
def test_operator_verb_forwards_argv_to_its_seam(
    monkeypatch: pytest.MonkeyPatch, seam: Seam
) -> None:
    """Each single-seam verb forwards its parsed argv to the right delegate and returns its value.

    seam: the verb-under-test with its seam location, forwarded call, and expected forwarding.
    """
    recorder = Recorder(ret=seam.ret)
    monkeypatch.setattr(seam.owner, seam.attr, recorder)

    out = dbutil.run(seam.call())

    assert out == seam.ret
    assert recorder.args == seam.args
    assert recorder.kwargs == seam.kwargs


def test_scale_forwards_sizes_and_wraps_the_recall_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scale forwards the size sweep and folds the tail budget into a `Budget`."""
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
    """The profile report is the span collector's stats, read straight through, slowest first."""
    stats = ["span-a", "span-b"]

    class Collector:
        def stats(self) -> list[str]:
            return stats

    monkeypatch.setattr(admin, "default_collector", lambda: Collector())

    assert admin.profile_report() is stats


def test_ingest_resolves_scopes_then_owns_the_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingest maps the org ids to a scope set, then owns the walk under the system user.

    The operator names its target orgs by Logto id, so `scopes_from_org_ids` (not the caller-side
    name resolver) turns them into the scope set the walk is stamped with.
    """
    resolved = SyncRecorder(ret=("SCOPE-SET",))
    ingest = Recorder(ret=4)
    monkeypatch.setattr(admin, "scopes_from_org_ids", resolved)
    monkeypatch.setattr(admin.extract_ingest, "ingest_path", ingest)

    out = dbutil.run(admin.ingest("notes/dir", scopes="org_team"))

    assert out == 4
    assert resolved.args == ("org_team",)
    assert ingest.args == (Path("notes/dir"),)
    assert ingest.kwargs == {"owner_id": SYSTEM, "scopes": ("SCOPE-SET",)}


def test_ingest_image_resolves_scopes_then_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingest-image maps the org ids and hands the path, caption, and owner to the image lane."""
    resolved = SyncRecorder(ret=("SCOPE-SET",))
    ingest = Recorder(ret=DOC_A)
    monkeypatch.setattr(admin, "scopes_from_org_ids", resolved)
    monkeypatch.setattr(admin.extract_ingest, "ingest_image", ingest)

    out = dbutil.run(admin.ingest_image("pic.png", caption="a cat", scopes="org_team"))

    assert out == DOC_A
    assert resolved.args == ("org_team",)
    assert ingest.args == (Path("pic.png"),)
    assert ingest.kwargs == {"caption": "a cat", "owner_id": SYSTEM, "scopes": ("SCOPE-SET",)}


@pytest.mark.parametrize("with_file", [False, True], ids=["synthesized", "from-file"])
def test_bench_reads_questions_then_runs_the_eval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, with_file: bool
) -> None:
    """Bench reads a questions file into lines, or passes null to let the eval synthesize its own.

    with_file: whether a questions file is supplied, exercising both `_read_questions` branches.
    """
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
    assert recorder.kwargs == {"k": 5, "user_id": SYSTEM}


@pytest.mark.parametrize(
    ("dims", "expected"), [(None, []), ("256,512", [256, 512])], ids=["live-width", "explicit"]
)
def test_sweep_parses_dims_into_the_matrix(
    monkeypatch: pytest.MonkeyPatch, dims: str | None, expected: list[int]
) -> None:
    """Sweep parses the comma widths into the sweep matrix, empty for the live width.

    dims: the comma-separated widths argument.
    expected: the parsed Matryoshka widths the matrix must carry.
    """
    recorder = Recorder(ret="SWEEP")
    monkeypatch.setattr(admin, "run_sweep", recorder)

    out = dbutil.run(admin.sweep(dims=dims, k=3))

    matrix = recorder.kwargs["matrix"]
    assert isinstance(matrix, SweepMatrix)
    assert out == "SWEEP"
    assert recorder.args[0] is None  # no questions file, synthesized
    assert matrix.embed_dim == expected
    assert recorder.kwargs["k"] == 3 and recorder.kwargs["user_id"] == SYSTEM


def test_benchmark_loads_the_gold_then_sweeps_when_enabled(
    monkeypatch: pytest.MonkeyPatch, settings: object
) -> None:
    """With the engine on, benchmark loads the named dataset to gold and sweeps against it."""
    monkeypatch.setattr(settings, "benchmarks_enabled", True)
    seen: dict[str, object] = {}

    def loader(path: Path) -> str:
        seen["loader"] = path
        return "DATASET"

    def benchmark_gold(dataset: str) -> str:
        seen["gold"] = dataset
        return "GOLD"

    sweep = Recorder(ret="BENCHMARK")
    monkeypatch.setitem(admin.benchmarks.LOADERS, "evermembench", loader)
    monkeypatch.setattr(admin.benchmarks, "benchmark_gold", benchmark_gold)
    monkeypatch.setattr(admin, "run_sweep", sweep)

    out = dbutil.run(admin.benchmark("evermembench", "ds.jsonl", k=6))

    assert out == "BENCHMARK"
    assert seen["loader"] == Path("ds.jsonl") and seen["gold"] == "DATASET"
    assert sweep.args[0] is None
    assert sweep.kwargs == {"k": 6, "user_id": SYSTEM, "gold": "GOLD"}


async def _catalog_row(sql: str, name: str) -> tuple[str, str] | None:
    """The (description, domain) of one ontology catalog row read past row level security."""
    async with dbutil.admin_engine().begin() as connection:
        row = (await connection.execute(text(sql), {"name": name})).first()
    return (row[0], row[1]) if row else None


def test_define_entity_and_relation_kind_write_the_catalog(
    migrated_db: None, fake_embedder: object
) -> None:
    """Defining a kind writes the canonical catalog row, and a repeat over it refines the gloss.

    The embedder behind the snapshot refresh is faked so the definition never reaches the network,
    and the two kinds are deleted afterward since the catalog is grow-only and never truncated
    between tests, restoring the process-wide ontology snapshot on the way out.
    """

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
            async with as_system():
                await ontology.refresh()

    entity, relation, refined = dbutil.run(run())

    assert entity == ("a widget gloss", "coding")  # canonicalized name, verbatim gloss and domain
    assert relation == ("x powers y", "research")
    assert refined == ("a sharper gloss", "general")  # a repeat refines rather than duplicates


def test_list_ontology_reports_kinds_with_live_use_counts(migrated_db: None) -> None:
    """The catalog surface reports every kind, entities before relations, each with its live uses.

    A single system-owned entity and fact are seeded against existing kinds so their use counts are
    non-zero, proving the group-by join maps counts onto the right catalog rows.
    """

    async def run() -> list[admin.OntologyKindRow]:
        await dbutil.reset_db()
        async with acting_as(SYSTEM) as session:
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
