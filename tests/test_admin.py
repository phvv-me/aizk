from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import dbutil
import pytest
import seedgraph
from bg_doubles import fake_runtime
from doubles import AsyncContext, FakeLLM, RecordingEmbedder
from id_factory import uuid5, uuid7
from pydantic import UUID5, UUID7
from sqlalchemy import text
from sqlalchemy.sql.selectable import Select

import aizk.admin as admin
from aizk.background.status import TasksStatus
from aizk.config import settings
from aizk.export import ExportReport
from aizk.extract.extractor import Extractor
from aizk.extract.models import ExtractedEntity, Extraction, TimedFact
from aizk.ontology import Ontology
from aizk.ops import HealthReport, ResetReport, SetupReport
from aizk.store import Relation
from aizk.store.identity import User

DOC_A = uuid7()
DOC_B = uuid7()
ACTOR = uuid5()
SYSTEM = settings.system_user_id
SENTINEL = "sentinel"

type RecordedValue = (
    str
    | int
    | float
    | Path
    | UUID5
    | UUID7
    | User
    | list[UUID5 | UUID7]
    | frozenset[UUID5 | UUID7]
    | None
)
type MaintenanceResult = int | tuple[int, int]
type SeamResult = str | ExportReport | TasksStatus | SetupReport | HealthReport | ResetReport


class Recorder[ReturnT]:
    def __init__(self, ret: ReturnT) -> None:
        self.ret = ret
        self.args: tuple[RecordedValue, ...] = ()
        self.kwargs: dict[str, RecordedValue] = {}

    async def __call__(self, *args: RecordedValue, **kwargs: RecordedValue) -> ReturnT:
        self.args = args
        self.kwargs = kwargs
        return self.ret


def test_system_is_the_configured_system_user() -> None:
    assert admin.system() == settings.system_user_id


@pytest.mark.parametrize(
    ("fn", "delegate", "ret", "expected"),
    [
        ("rebuild", "build_graph", (3, 5), (3, 5)),
        ("decay", "decay", 7, 7),
        ("reembed", "reembed", 9, 9),
        ("communities", "build_communities", 6, 6),
        ("raptor", "build_raptor", 4, 4),
    ],
)
@pytest.mark.parametrize("user_id", [None, ACTOR], ids=["default", "explicit"])
def test_maintenance_op_defaults_to_the_system_user(
    monkeypatch: pytest.MonkeyPatch,
    fn: str,
    delegate: str,
    ret: MaintenanceResult,
    expected: MaintenanceResult,
    user_id: UUID5 | UUID7 | None,
) -> None:
    recorder = Recorder(ret=ret)
    monkeypatch.setattr(admin.graph, delegate, recorder)
    extras: dict[str, tuple[object, ...]] = {
        "rebuild": (fake_runtime().graph,),
        "raptor": (FakeLLM().llm, RecordingEmbedder()),
    }

    out = dbutil.run(getattr(admin, fn)(*extras.get(fn, ()), user_id=user_id))

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

        async def exec(self, statement: Select) -> list[UUID5 | UUID7] | list[str | None]:
            self.calls += 1
            return [DOC_A, DOC_B] if self.calls == 1 else ["Note A", None]

    def fake_transaction(user: User) -> AsyncContext[FakeSession]:
        return AsyncContext(FakeSession())

    async def fake_forget(
        session: FakeSession, doc_ids: list[UUID5 | UUID7]
    ) -> list[UUID5 | UUID7]:
        return doc_ids  # every named document contributed one live claim

    monkeypatch.setattr(
        admin.EmbedClient,
        "from_settings",
        classmethod(lambda cls, config: SimpleNamespace(embed=fake_embed)),
    )
    monkeypatch.setattr(User, "app", property(fake_transaction))
    monkeypatch.setattr(admin.Fact.Claim, "forget_from_documents", fake_forget)

    result = dbutil.run(admin.forget("a wrong note", k=8))

    assert result.claims == 2  # both ranked documents' claims retracted
    assert result.documents == ["Note A"]  # the null title dropped, the real one kept


def test_audit_lists_the_recent_visible_writes(migrated_db: None) -> None:
    async def run() -> tuple[set[UUID5 | UUID7], set[UUID5 | UUID7]]:
        await dbutil.reset_db()
        first = await dbutil.seed_document(SYSTEM, [SYSTEM])
        second = await dbutil.seed_document(SYSTEM, [SYSTEM])
        docs = await admin.audit(limit=10)
        return {doc.id for doc in docs}, {first, second}

    seen, expected = dbutil.run(run())
    assert seen == expected


def test_diagnose_extraction_reads_one_chunk_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "Aizk uses PostgreSQL."
    chunk = SimpleNamespace(id=DOC_A, document_id=DOC_B, text=source, provenance={})
    document = SimpleNamespace(title="AIZK")
    extraction = Extraction(
        entities=[
            ExtractedEntity(name="Aizk", type="Project"),
            ExtractedEntity(name="PostgreSQL", type="Tool"),
        ],
        facts=[
            TimedFact(
                subject="Aizk",
                predicate="uses",
                object="PostgreSQL",
                statement="Aizk uses PostgreSQL.",
                quote=source,
            )
        ],
    )

    class FakeSession:
        async def get(self, model: type, identifier: UUID7) -> SimpleNamespace | None:
            assert identifier in {DOC_A, DOC_B}
            return chunk if model is admin.Chunk else document

    extractor = SimpleNamespace(extract=AsyncMock(return_value=extraction))
    ensure = AsyncMock()
    monkeypatch.setattr(User, "owner", property(lambda user: AsyncContext(FakeSession())))
    monkeypatch.setattr(admin.Ontology, "ensure", ensure)

    report = dbutil.run(admin.diagnose_extraction(cast("Extractor", extractor), DOC_A))

    assert report.document_title == "AIZK"
    assert report.source_chars == len(source)
    assert report.grounding[0].rejection is None
    assert report.accepted.quality.accepted_facts == 1
    assert extractor.extract.call_args.args == (source,)
    assert ensure.call_count == 1


def test_diagnose_extraction_rejects_an_unknown_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        async def get(self, model: type, identifier: UUID7) -> None:
            return None

    monkeypatch.setattr(User, "owner", property(lambda user: AsyncContext(FakeSession())))

    with pytest.raises(ValueError, match="unknown chunk"):
        dbutil.run(admin.diagnose_extraction(cast("Extractor", None), DOC_A))


@dataclass
class Seam:
    id: str
    owner: ModuleType
    attr: str
    ret: str
    call: Callable[[], Awaitable[SeamResult]]
    args: tuple[RecordedValue, ...]
    kwargs: Mapping[str, RecordedValue]


SEAMS: list[Seam] = [
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
    Seam("reset", admin.ops, "reset", SENTINEL, admin.reset_database, (), {}),
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


def test_ingest_resolves_scopes_for_the_text_admin_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = admin.settings.scope_id("org_team")
    ingest = Recorder(ret=4)
    monkeypatch.setattr(admin.extract_ingest, "ingest_path", ingest)

    out = dbutil.run(admin.ingest("notes/dir", scopes="org_team"))

    assert out == 4
    assert ingest.args == (User.system(frozenset({scope})), Path("notes/dir"))
    assert ingest.kwargs == {
        "created_by": SYSTEM,
        "scopes": frozenset({scope}),
    }


async def _catalog_row(sql: str, name: str) -> tuple[str, str] | None:
    async with dbutil.admin_engine().begin() as connection:
        row = (await connection.execute(text(sql), {"name": name})).first()
    return (row[0], row[1]) if row is not None else None


def test_define_entity_and_relation_kind_write_the_catalog(
    migrated_db: None, fake_embedder: RecordingEmbedder
) -> None:
    async def run() -> tuple[
        tuple[str, str] | None,
        tuple[str, str] | None,
        tuple[str, str] | None,
        Relation.Policy,
    ]:
        await dbutil.reset_db()
        try:
            await admin.define_entity_kind("TestWidget", "a widget gloss", "coding")
            await admin.define_relation_kind(
                "test_powers",
                "x powers y",
                "research",
                Relation.Policy.state,
            )
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
            async with User.system() as session:
                policy = (await session.get_one(Relation.Kind, "test_powers")).policy
            return entity, relation, refined, policy
        finally:
            await dbutil.admin_exec("DELETE FROM entity_kind WHERE name = 'test_widget'")
            await dbutil.admin_exec("DELETE FROM relation_kind WHERE name = 'test_powers'")
            async with User.system() as session:
                await Ontology.refresh(session)

    entity, relation, refined, policy = dbutil.run(run())

    assert entity == ("a widget gloss", "coding")  # canonicalized name, verbatim gloss and domain
    assert relation == ("x powers y", "research")
    assert policy == Relation.Policy.state
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
