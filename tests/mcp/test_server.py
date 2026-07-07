import uuid
from types import SimpleNamespace

import dbutil
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import FunctionTool
from hypothesis import given
from hypothesis import strategies as st
from mcp_probe import FakeSession, FakeSystemSession, const, fake_system_session

import aizk.mcp.server as server_module
from aizk.config import settings
from aizk.exceptions import NotGroupAdminError, ScopeNotFoundError
from aizk.mcp.models import (
    DecayResult,
    GraphBuildResult,
    GroupCreated,
    GroupDeleted,
    GroupFlag,
    GroupSummary,
    IngestResult,
    MembershipChange,
    PendingFact,
    PrincipalSummary,
    PromoteResult,
    RaptorBuildResult,
    ReembedResult,
    ReviewResult,
    WriteRecord,
    WriteResult,
)
from aizk.mcp.principal import ADMIN_TAG, Principal
from aizk.mcp.server import (
    AizkMCP,
    parse_fact_ids,
    resolve_group_admin,
    resolve_scopes,
    server,
    startup_check,
)

pytestmark = pytest.mark.usefixtures("migrated_db")


def apply_patches(monkeypatch: pytest.MonkeyPatch, patches: dict[str, object]) -> None:
    """Install each `patches` seam, a dotted path resolving to a submodule attribute else `server`.

    monkeypatch: the active patcher whose reverts restore the seams after the test.
    patches: seam path to its stand-in, `graph.decay` on the `graph` submodule, `run_eval` on the
        server module itself.
    """
    for path, fake in patches.items():
        module_name, _, attr = path.rpartition(".")
        target = getattr(server_module, module_name) if module_name else server_module
        monkeypatch.setattr(target, attr, fake)


# the registration contract: the memory verbs a caller reaches gated in-body, versus the admin
# surface `admin_tool` tags so the listing hides it. The two disjoint partitions the server keeps.
USER_TOOLS = {
    "recall",
    "remember",
    "reference",
    "pending",
    "approve",
    "reject",
}
ADMIN_TOOLS = {
    "force_rebuild",
    "forget",
    "force_decay",
    "force_reembed",
    "force_raptor",
    "bench",
    "sweep",
    "benchmark",
    "scale",
    "ingest",
    "ingest_image",
    "promote",
    "export_scope",
    "profile_report",
    "tasks_status",
    "setup",
    "health",
    "create_user",
    "grant_admin",
    "define_entity_kind",
    "define_relation_kind",
    "list_ontology",
    "create_group",
    "add_member",
    "remove_member",
    "publish_group",
    "curate_group",
    "delete_group",
    "list_groups",
    "list_principals",
    "audit",
}


def test_registration_partitions_the_verbs_from_the_tagged_admin_surface(
    tools: dict[str, FunctionTool],
) -> None:
    """The server exposes the memory verbs untagged and tags exactly the operational surface."""
    tagged = {name for name, tool in tools.items() if ADMIN_TAG in tool.tags}
    assert set(tools) >= USER_TOOLS
    assert tagged == ADMIN_TOOLS  # every operational tool is tagged, no memory verb ever is
    assert not (tagged & USER_TOOLS)


def test_init_wires_a_verifier_and_the_rate_limit_on_the_configured_http_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an issuer configured and HTTP on, `__init__` attaches the verifier and the rate limit.

    Drives both `__init__` branches the stdio single-user default skips: a resolving `verifier()`
    hands the auth provider to FastMCP, and the shared HTTP transport composes the anonymous rate
    limit onto the middleware stack.
    """
    from aizk.mcp.middleware import AnonymousRateLimit

    monkeypatch.setattr(settings, "zitadel_issuer", "https://issuer.test/aizk")
    monkeypatch.setattr(settings, "zitadel_jwks_url", "https://issuer.test/jwks")
    monkeypatch.setattr(settings, "zitadel_introspect_url", "")
    monkeypatch.setattr(settings, "mcp_http", True)

    probe = AizkMCP("probe")

    assert probe.auth is not None
    assert any(isinstance(mw, AnonymousRateLimit) for mw in probe.middleware)


@pytest.mark.parametrize(
    ("profiling", "auto_setup", "up_to_date"),
    [(True, True, True), (False, True, False), (False, False, False)],
    ids=["profiling-and-current", "behind-runs-setup", "opted-out"],
)
def test_startup_check_enables_spans_and_auto_setups_only_when_behind(
    monkeypatch: pytest.MonkeyPatch, profiling: bool, auto_setup: bool, up_to_date: bool
) -> None:
    """The lifespan turns spans on when profiling, and runs setup only for a schema behind head."""
    spans_enabled: list[bool] = []
    setups: list[bool] = []
    monkeypatch.setattr(settings, "profiling", profiling)
    monkeypatch.setattr(settings, "auto_setup", auto_setup)
    monkeypatch.setattr(server_module, "enable_spans", lambda: spans_enabled.append(True))
    report = SimpleNamespace(migration=SimpleNamespace(up_to_date=up_to_date))
    applied = SimpleNamespace(migrated_from="a", migrated_to="b")

    async def fake_setup() -> SimpleNamespace:
        setups.append(True)
        return applied

    monkeypatch.setattr(server_module.ops, "health", const(report))
    monkeypatch.setattr(server_module.ops, "setup", fake_setup)

    async def drive() -> None:
        async with startup_check(server):
            pass

    dbutil.run(drive())
    assert spans_enabled == ([True] if profiling else [])
    # setup runs exactly when auto-setup is on and the health read finds the schema behind head
    assert setups == ([True] if (auto_setup and not up_to_date) else [])


@pytest.mark.parametrize(
    "blank", [None, "", "   ", " , ,"], ids=["null", "empty", "spaces", "commas"]
)
def test_resolve_scopes_maps_a_blank_string_to_the_private_lens(blank: str | None) -> None:
    """A null or blank scope string means private, the empty tuple, never a database lookup."""
    assert dbutil.run(resolve_scopes(blank, uuid.uuid4())) == ()


def test_resolve_scopes_canonicalizes_names_to_a_sorted_id_tuple_and_fails_on_an_unknown() -> None:
    """Any order of known names resolves to one sorted id tuple; an unknown name fails fast."""

    async def probe() -> None:
        await dbutil.reset_db()
        principal_id = await dbutil.seed_principal(uuid.uuid4())
        ids = {
            name: await dbutil.seed_group(uuid.uuid4(), name=name)
            for name in ("alpha", "beta", "gamma")
        }
        canonical = tuple(sorted(ids.values()))
        assert await resolve_scopes("beta,alpha,gamma", principal_id) == canonical
        assert await resolve_scopes("gamma, beta ,alpha", principal_id) == canonical
        with pytest.raises(ScopeNotFoundError, match="no scope"):
            await resolve_scopes("ghost", principal_id)

    dbutil.run(probe())


@given(ids=st.lists(st.uuids(), max_size=6))
def test_parse_fact_ids_round_trips_a_comma_list_and_ignores_stray_whitespace(
    ids: list[uuid.UUID],
) -> None:
    """The id parser recovers exactly the ids from a padded comma list, dropping empty fields."""
    rendered = " , ".join(f" {fact} " for fact in ids) + " ,"
    assert parse_fact_ids(rendered) == ids
    assert parse_fact_ids("") == []


@pytest.mark.parametrize("vetted", [True, False], ids=["admin", "non-admin"])
def test_resolve_group_admin_returns_the_group_or_wraps_a_domain_refusal(
    monkeypatch: pytest.MonkeyPatch, vetted: bool
) -> None:
    """A vetted caller gets the group back; a non-admin reads a plain ToolError, not the domain."""
    monkeypatch.setattr(
        server_module, "current_principal", lambda: Principal(id=uuid.uuid4(), is_admin=False)
    )

    class FakeGroup:
        async def require_admin(self, session: object, principal_id: uuid.UUID) -> None:
            if not vetted:
                raise NotGroupAdminError("not your group")

    fake_group = FakeGroup()
    monkeypatch.setattr(server_module.Group, "named", const(fake_group))
    if vetted:
        assert dbutil.run(resolve_group_admin(object(), "team")) is fake_group
    else:
        with pytest.raises(ToolError, match="not your group"):
            dbutil.run(resolve_group_admin(object(), "team"))


def test_recall_forwards_the_query_budget_and_resolved_lens_to_the_context_pack(
    monkeypatch: pytest.MonkeyPatch, as_admin: Principal, tools: dict[str, FunctionTool]
) -> None:
    """The one recall verb forwards query, budget, resolved lens, and caller to the pack builder.

    Recall is now the single retrieval verb and returns the assembled context pack rather than a
    raw result, so it delegates to `assemble_context_pack`, not the lower-level `recall` lane.
    """
    captured: dict[str, object] = {}
    sentinel = object()

    async def stub(query: str, **kwargs: object) -> object:
        captured.update(query=query, token_budget=kwargs["token_budget"], scopes=kwargs["scopes"])
        captured["principal_id"] = kwargs["principal_id"]
        return sentinel

    monkeypatch.setattr(server_module.retrieval, "assemble_context_pack", stub)
    out = dbutil.run(tools["recall"].fn(query="what holds", scopes=None, budget=2000))
    assert out is sentinel
    assert captured == {
        "query": "what holds",
        "token_budget": 2000,
        "scopes": (),  # a null scope string resolves to the empty private lens
        "principal_id": as_admin.id,
    }


def test_remember_writes_under_the_identified_caller_and_returns_the_id(
    monkeypatch: pytest.MonkeyPatch, as_admin: Principal, tools: dict[str, FunctionTool]
) -> None:
    """The remember verb writes under the identified caller and the resolved lens, id back."""
    item_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def stub(text: str, **kwargs: object) -> uuid.UUID:
        captured.update(
            text=text, kind=kwargs["kind"], owner_id=kwargs["owner_id"], scopes=kwargs["scopes"]
        )
        return item_id

    monkeypatch.setattr(server_module.extract_ingest, "remember_session", stub)
    out = dbutil.run(tools["remember"].fn(text="a decision", scopes=None, kind="code"))
    assert out == WriteResult(id=item_id)
    assert captured == {
        "text": "a decision",
        "kind": "code",
        "owner_id": as_admin.id,
        "scopes": (),
    }


def body_cases() -> list[tuple[str, dict[str, object], dict[str, object], object]]:
    """Each verb with its faked seams, call kwargs, and the exact result its body returns.

    An `object()` sentinel is the pass-through verbs' faked delegate result, asserted returned
    unchanged; a constructed model is the wrapping verbs' promised shape. Every body runs under the
    admin caller with `scopes=None`, so `resolve_scopes` yields the empty lens with no database and
    the delegate or `Group`/`Principal` classmethod under a faked `system_session` is the seam.
    """
    pid, did, new_id = str(uuid.uuid4()), str(uuid.uuid4()), uuid.uuid4()
    scaler, expr = object(), object()
    tasks, setupr, healthr = object(), object(), object()
    fact = SimpleNamespace(
        id=uuid.uuid4(), owner_id=uuid.uuid4(), predicate="knows", statement="a claim"
    )
    write = SimpleNamespace(
        id=new_id, kind="note", owner_id=new_id, scopes=[], promoted_from=None, title="a note"
    )
    created = SimpleNamespace(id=new_id, display_name="bob")
    return [
        (
            "reference",
            {"extract_ingest.record_reference": const(new_id)},
            {"uri": "u"},
            WriteResult(id=new_id),
        ),
        (
            "force_rebuild",
            {"graph.build_graph": const((3, 5))},
            {},
            GraphBuildResult(entities=3, facts=5),
        ),
        ("force_decay", {"graph.decay": const(7)}, {}, DecayResult(archived=7)),
        ("force_reembed", {"graph.reembed": const(9)}, {}, ReembedResult(written=9)),
        ("force_raptor", {"graph.build_raptor": const(4)}, {}, RaptorBuildResult(written=4)),
        ("scale", {"run_scale_benchmark": const(scaler)}, {"sizes": "100,200"}, scaler),
        ("export_scope", {"export.export_scope": const(expr)}, {"path": "d.jsonl"}, expr),
        ("tasks_status", {"tasks_overview": const(tasks)}, {}, tasks),
        ("setup", {"ops.setup": const(setupr)}, {}, setupr),
        ("health", {"ops.health": const(healthr)}, {}, healthr),
        (
            "profile_report",
            {"default_collector": lambda: SimpleNamespace(stats=lambda: [])},
            {},
            SimpleNamespace(stats=[]),
        ),
        (
            "ingest",
            {"extract_ingest.ingest_path": const(2)},
            {"path": "notes"},
            IngestResult(count=2, path="notes"),
        ),
        (
            "ingest_image",
            {"extract_ingest.ingest_image": const(new_id)},
            {"path": "a.png"},
            WriteResult(id=new_id),
        ),
        (
            "promote",
            {"graph.promote": const(6)},
            {"document": did, "to_scopes": "team"},
            PromoteResult(promoted=6, to_scopes="team"),
        ),
        (
            "create_user",
            {"system_session": fake_system_session, "PrincipalRow.create": const(created)},
            {"name": "bob"},
            PrincipalSummary(id=new_id, display_name="bob", is_admin=False),
        ),
        (
            "create_group",
            {
                "system_session": fake_system_session,
                "Group.create": const(SimpleNamespace(id=new_id)),
            },
            {"name": "team"},
            GroupCreated(id=new_id),
        ),
        (
            "add_member",
            {
                "system_session": fake_system_session,
                "Group.named": const(SimpleNamespace(add_member=const(None))),
            },
            {"principal": pid, "group": "team", "role": "reader"},
            MembershipChange(principal=uuid.UUID(pid), group="team", role="reader"),
        ),
        (
            "remove_member",
            {
                "system_session": fake_system_session,
                "Group.named": const(SimpleNamespace(remove_member=const(None))),
            },
            {"principal": pid, "group": "team"},
            MembershipChange(principal=uuid.UUID(pid), group="team"),
        ),
        (
            "publish_group",
            {
                "system_session": fake_system_session,
                "Group.named": const(SimpleNamespace(publish=const(None))),
            },
            {"group": "team"},
            GroupFlag(group="team", public=True),
        ),
        (
            "curate_group",
            {
                "system_session": fake_system_session,
                "Group.named": const(SimpleNamespace(curate=const(None))),
            },
            {"group": "team"},
            GroupFlag(group="team", curated=True),
        ),
        (
            "delete_group",
            {
                "system_session": fake_system_session,
                "Group.named": const(SimpleNamespace(delete=const(None))),
            },
            {"group": "team"},
            GroupDeleted(group="team"),
        ),
        (
            "list_groups",
            {
                "system_session": fake_system_session,
                "Group.list_all": const([{"name": "team", "public": True, "members": 2}]),
            },
            {},
            [GroupSummary(name="team", public=True, members=2)],
        ),
        (
            "list_principals",
            {
                "system_session": fake_system_session,
                "PrincipalRow.list_all": const(
                    [SimpleNamespace(id=new_id, display_name="Alice", is_admin=False)]
                ),
            },
            {},
            [PrincipalSummary(id=new_id, display_name="Alice", is_admin=False)],
        ),
        (
            "audit",
            {"PrincipalRow.recent_writes": const([write])},
            {},
            [
                WriteRecord(
                    id=new_id,
                    kind="note",
                    owner_id=new_id,
                    scopes=[],
                    promoted_from=None,
                    title="a note",
                )
            ],
        ),
        (
            "pending",
            {
                "system_session": fake_system_session,
                "resolve_group_admin": const(SimpleNamespace(pending_facts=const([fact]))),
            },
            {"group": "team"},
            [
                PendingFact(
                    id=fact.id, owner_id=fact.owner_id, predicate="knows", statement="a claim"
                )
            ],
        ),
        (
            "approve",
            {
                "system_session": fake_system_session,
                "resolve_group_admin": const(SimpleNamespace(approve_facts=const(2))),
            },
            {"group": "team", "facts": "all"},
            ReviewResult(group="team", count=2),
        ),
        (
            "approve",
            {
                "system_session": fake_system_session,
                "resolve_group_admin": const(SimpleNamespace(approve_facts=const(1))),
            },
            {"group": "team", "facts": str(uuid.uuid4())},
            ReviewResult(group="team", count=1),
        ),
        (
            "reject",
            {
                "system_session": fake_system_session,
                "resolve_group_admin": const(SimpleNamespace(reject_facts=const(3))),
            },
            {"group": "team", "facts": f"{uuid.uuid4()}, {uuid.uuid4()}"},
            ReviewResult(group="team", count=3),
        ),
    ]


@pytest.mark.parametrize(
    ("tool_name", "patches", "kwargs", "expected"),
    body_cases(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_bodies_build_their_delegate_call_and_return_the_promised_model(
    monkeypatch: pytest.MonkeyPatch,
    as_admin: Principal,
    tools: dict[str, FunctionTool],
    tool_name: str,
    patches: dict[str, object],
    kwargs: dict[str, object],
    expected: object,
) -> None:
    """Each body resolves the admin caller, delegates to its faked seam, and returns the model.

    A pass-through verb returns its delegate's object unchanged (asserted identical); a wrapping
    verb returns the constructed result model. The admin gate runs for every tagged tool since
    `.fn` is the gated wrapper, so an admin caller drives the body while the gate refusing a
    non-admin is covered separately.
    """
    apply_patches(monkeypatch, patches)
    out = dbutil.run(tools[tool_name].fn(**kwargs))
    if isinstance(expected, SimpleNamespace):  # profile_report's ProfileReport(stats=[]) shape
        assert out.stats == expected.stats
    else:
        assert out == expected


def test_bench_and_sweep_read_the_questions_file_and_matryoshka_dims(
    monkeypatch: pytest.MonkeyPatch,
    as_admin: Principal,
    tools: dict[str, FunctionTool],
    tmp_path: object,
) -> None:
    """Both admin evals read a questions file when named and synthesize otherwise, sweep dims too.

    Drives every arm of the two `... if <file/dims> else ...` expressions: bench with and without a
    file, sweep with dims and no file then no dims and a file, asserting the argv each body built.
    """
    from pathlib import Path

    captured: dict[str, object] = {}

    async def stub_eval(questions: object, **kwargs: object) -> object:
        captured["bench_questions"] = questions
        return object()

    async def stub_sweep(questions: object, **kwargs: object) -> object:
        captured["sweep_questions"] = questions
        captured["matrix"] = kwargs["matrix"].embed_dim
        return object()

    monkeypatch.setattr(server_module, "run_eval", stub_eval)
    monkeypatch.setattr(server_module, "run_sweep", stub_sweep)
    qfile = Path(tmp_path) / "q.txt"  # type: ignore[arg-type]
    qfile.write_text("one\ntwo\n", encoding="utf-8")

    dbutil.run(tools["bench"].fn(questions_file=None, k=3))
    assert captured["bench_questions"] is None
    dbutil.run(tools["bench"].fn(questions_file=str(qfile), k=3))
    assert captured["bench_questions"] == ["one", "two"]

    dbutil.run(tools["sweep"].fn(questions_file=None, k=3, dims="512,1024"))
    assert captured["sweep_questions"] is None
    assert captured["matrix"] == [512, 1024]
    dbutil.run(tools["sweep"].fn(questions_file=str(qfile), k=3, dims=None))
    assert captured["sweep_questions"] == ["one", "two"]
    assert captured["matrix"] == []  # no dims named leaves the Matryoshka width sweep empty


def test_grant_admin_reports_the_promoted_principal_or_refuses_an_unknown_id(
    monkeypatch: pytest.MonkeyPatch, as_admin: Principal, tools: dict[str, FunctionTool]
) -> None:
    """A resolvable id is promoted and reported admin; an unresolvable one fails fast, no no-op."""
    target = SimpleNamespace(id=uuid.uuid4(), display_name="dana", grant_admin=const(None))
    monkeypatch.setattr(
        server_module, "system_session", lambda: FakeSystemSession(FakeSession(target))
    )
    out = dbutil.run(tools["grant_admin"].fn(principal=str(target.id)))
    assert out == PrincipalSummary(id=target.id, display_name="dana", is_admin=True)

    monkeypatch.setattr(
        server_module, "system_session", lambda: FakeSystemSession(FakeSession(None))
    )
    with pytest.raises(ToolError, match="no principal"):
        dbutil.run(tools["grant_admin"].fn(principal=str(uuid.uuid4())))


def test_benchmark_guards_the_disabled_engine_and_an_unknown_dataset(
    monkeypatch: pytest.MonkeyPatch, as_admin: Principal, tools: dict[str, FunctionTool]
) -> None:
    """The benchmark tool refuses when off, rejects an unknown name, then runs the known sweep."""
    sentinel = object()
    monkeypatch.setattr(settings, "benchmarks_enabled", False)
    with pytest.raises(ToolError, match="benchmarks are off"):
        dbutil.run(tools["benchmark"].fn(name="evermembench", dataset_path="x.jsonl"))

    monkeypatch.setattr(settings, "benchmarks_enabled", True)
    monkeypatch.setattr(server_module.benchmarks, "LOADERS", {"evermembench": lambda path: []})
    monkeypatch.setattr(server_module.benchmarks, "benchmark_gold", lambda rows: rows)
    monkeypatch.setattr(server_module, "run_sweep", const(sentinel))
    with pytest.raises(ValueError, match="unknown benchmark"):
        dbutil.run(tools["benchmark"].fn(name="nope", dataset_path="x.jsonl"))
    out = dbutil.run(tools["benchmark"].fn(name="evermembench", dataset_path="x.jsonl"))
    assert out is sentinel


@pytest.mark.parametrize("tool_name", ["list_groups", "force_decay"])
def test_admin_gate_refuses_a_non_admin_even_through_a_direct_body_call(
    monkeypatch: pytest.MonkeyPatch, tools: dict[str, FunctionTool], tool_name: str
) -> None:
    """The `admin_tool` wrapper refuses a non-admin before the body runs, bypassing the listing."""
    monkeypatch.setattr(
        server_module, "current_principal", lambda: Principal(id=uuid.uuid4(), is_admin=False)
    )
    with pytest.raises(ToolError, match="admin principal"):
        dbutil.run(tools[tool_name].fn())


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [("remember", {"text": "t", "scopes": None}), ("reference", {"uri": "u", "scopes": None})],
)
def test_write_verbs_refuse_the_anonymous_caller(
    monkeypatch: pytest.MonkeyPatch,
    tools: dict[str, FunctionTool],
    tool_name: str,
    kwargs: dict[str, object],
) -> None:
    """A write verb refuses the anonymous read-only principal before it ever touches storage."""
    monkeypatch.setattr(
        server_module,
        "current_principal",
        lambda: Principal(id=settings.anonymous_principal_id, is_admin=False),
    )
    with pytest.raises(ToolError, match="anonymous"):
        dbutil.run(tools[tool_name].fn(**kwargs))


def test_end_to_end_listing_hides_the_admin_surface_and_the_gate_refuses_through_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the real in-process client, a non-admin's listing hides the admin tools and a direct
    admin call is refused, while an admin sees and calls the surface, the whole stack wired."""
    monkeypatch.setattr(settings, "auto_setup", False)  # skip the queue-schema health probe

    async def drive() -> None:
        await dbutil.reset_db()
        await dbutil.seed_principal(settings.principal, is_admin=True)
        async with Client(server) as client:
            names = {t.name for t in await client.list_tools()}
            assert names >= ADMIN_TOOLS  # an admin sees the operational surface
            assert (await client.call_tool("list_principals", {})).data is not None

        await dbutil.reset_db()
        await dbutil.seed_principal(settings.principal, is_admin=False)
        async with Client(server) as client:
            names = {t.name for t in await client.list_tools()}
            assert not (ADMIN_TOOLS & names)  # a non-admin listing hides every admin tool
            assert names >= USER_TOOLS
            with pytest.raises(ToolError, match="admin principal"):
                await client.call_tool("list_principals", {})

    dbutil.run(drive())
