import asyncio
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.rate_limiting import RateLimitError
from graphdb import create_group
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from strategies import recall_results

import aizk.mcp.middleware as middleware_module
import aizk.mcp.principal as principal_module
import aizk.mcp.server as server_module
from aizk.config import settings
from aizk.exceptions import NotGroupAdminError, ScopeNotFoundError
from aizk.mcp.middleware import AnonymousRateLimit
from aizk.mcp.principal import (
    AUTH_TOKEN_ENV,
    bearer_token,
    caller_principal,
    require_admin,
    require_identified,
)
from aizk.mcp.server import (
    ADMIN_TAG,
    AizkMCP,
    parse_fact_ids,
    resolve_group_admin,
    resolve_scope,
    server,
)
from aizk.retrieval import CommunityNote, FactHit, Hit, RaptorNote, RecallResult

# the memory verbs and curation tools every caller reaches, gated in-body rather than hidden from
# listing, and the operational surface the admin gate protects; the two partitions the registration
# contract keeps disjoint and the admin tag carves apart
USER_TOOLS = {"recall", "remember", "reference", "get_context", "pending", "approve", "reject"}
ADMIN_TOOLS = {
    "force_rebuild",
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
    "tasks_status",
    "create_user",
    "grant_admin",
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

# identifiers for the generated probe tools, lowercase so they are valid MCP tool names
tool_names = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)


def text_of(result: object) -> str:
    """The rendered string a str-returning tool carries on its structured content.

    result: the ToolResult a `tool.run` resolved to.
    """
    content = getattr(result, "structured_content", None)
    assert isinstance(content, dict)
    return content["result"]


def make_probe(probe: AizkMCP, spec: dict[str, bool]) -> None:
    """Register each `spec` tool on `probe`, admin-gated through `admin_tool` where marked.

    spec: tool name to whether it is admin-gated, the surface `admin_tool` and plain `tool`
        partition between them.
    """
    for name, is_admin_tool in spec.items():

        async def body() -> str:
            return "ok"

        body.__name__ = name
        probe.admin_tool(body) if is_admin_tool else probe.tool(body)


def _const[T](value: T) -> Callable[..., Awaitable[T]]:
    """An async function ignoring its arguments and resolving to `value`, a seam stand-in.

    value: the constant the returned coroutine yields.
    """

    async def fixed(*args: object, **kwargs: object) -> T:
        return value

    return fixed


class FakeTarget:
    """A generic fetched-row stand-in for the one `session.get` an admin tool body still runs.

    id: the row id `session.get` callers read back.
    """

    def __init__(self, id_: uuid.UUID | None = None) -> None:
        self.id = id_ or uuid.uuid4()

    async def grant_admin(self, session: object) -> None:
        """No-op, the call the grant_admin tool body makes on the fetched row."""


class FakeSession:
    """A `system_session()` stand-in exposing only `.get`, the one raw session call an admin tool
    body still runs once `Group`/`Principal` classmethods take the rest of the flow.

    get_result: the row `.get(Model, id)` resolves to, null included, to drive the not-found
        branch a real missing id would take.
    """

    def __init__(self, get_result: object) -> None:
        self.get_result = get_result

    async def get(self, model: type, id_: object, **kwargs: object) -> object:
        return self.get_result


class FakeSystemSession:
    """An async context manager standing in for `system_session()`, no real database touched.

    session: the `FakeSession` the block acts under, a fresh `FakeTarget`-carrying one by default.
    """

    def __init__(self, session: FakeSession | None = None) -> None:
        self.session = session or FakeSession(FakeTarget())

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, *exc: object) -> bool:
        return False


def fake_system_session() -> FakeSystemSession:
    """Zero-arg stand-in for `system_session`, the shape every admin tool body calls it in."""
    return FakeSystemSession()


@given(
    spec=st.dictionaries(tool_names, st.booleans(), min_size=1, max_size=5),
    caller_is_admin=st.booleans(),
)
@hyp_settings(max_examples=25)
def test_admin_gate_filters_listing_and_refuses_call(
    monkeypatch: pytest.MonkeyPatch, spec: dict[str, bool], caller_is_admin: bool
) -> None:
    """The real middleware hides every admin tool from a non-admin, and `admin_tool` refuses it.

    Drives a fresh AizkMCP, its own PrincipalMiddleware wired by `__init__`, over a generated tool
    set, faking only the auth boundary, so for any partition of admin-gated and plain tools and
    either caller standing the listing shows exactly the visible set and a gated call passes for an
    admin yet raises for a non-admin, while a plain tool always passes (invariant 4, the admin
    gate), even through a direct `client.call_tool` that never consults the listing.
    """
    monkeypatch.setattr(middleware_module, "caller_principal", _const(uuid.uuid4()))
    monkeypatch.setattr(middleware_module.Principal, "administers", _const(caller_is_admin))
    probe = AizkMCP("probe")
    make_probe(probe, spec)
    admin_only = {name for name, tagged in spec.items() if tagged}
    plain = set(spec) - admin_only
    expected_visible = set(spec) if caller_is_admin else plain

    async def drive() -> None:
        async with Client(probe) as client:
            assert {tool.name for tool in await client.list_tools()} == expected_visible
            for name in plain:
                assert (await client.call_tool(name, {})).data == "ok"
            for name in admin_only:
                if caller_is_admin:
                    assert (await client.call_tool(name, {})).data == "ok"
                else:
                    with pytest.raises(ToolError, match="admin principal"):
                        await client.call_tool(name, {})

    asyncio.run(drive())


def test_real_server_registers_verbs_and_tags_the_admin_surface() -> None:
    """The module server exposes the three verbs untagged and tags the operational surface."""
    tools = asyncio.run(server.get_tools())
    tagged = {name for name, tool in tools.items() if ADMIN_TAG in tool.tags}
    assert set(tools) >= USER_TOOLS
    assert set(tools) >= ADMIN_TOOLS
    assert tagged == ADMIN_TOOLS  # every operational tool is tagged, no memory verb ever is
    assert not (tagged & USER_TOOLS)


def test_admin_gate_end_to_end_with_real_auth(
    requires_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against the real auth layer the root admin sees and runs the surface, a stranger neither.

    The default principal is the root, always an admin, so it lists the operational tools and a
    read-only one runs; a fresh random principal the auth layer reads as non-admin sees only the
    three verbs and is refused before an admin tool body runs (invariant 4, the live gate).
    """

    async def listed() -> set[str]:
        async with Client(server) as client:
            return {tool.name for tool in await client.list_tools()}

    assert asyncio.run(listed()) >= ADMIN_TOOLS
    monkeypatch.setattr(settings, "principal", uuid.uuid4())
    stranger = asyncio.run(listed())
    assert stranger == USER_TOOLS

    async def refuse() -> None:
        async with Client(server) as client:
            await client.call_tool("create_user", {"name": "mallory"})

    monkeypatch.setattr(settings, "principal", uuid.uuid4())
    with pytest.raises(ToolError, match="admin principal"):
        asyncio.run(refuse())


# the principal resolution precedence: a bearer token first, then the default, each source faked
# at the auth boundary so the order is asserted without any identity provider
TOKEN_PRINCIPAL = uuid.uuid4()


@pytest.mark.parametrize(
    ("token_env", "token_resolves", "expected"),
    [
        ("tok", True, TOKEN_PRINCIPAL),  # a resolving token wins outright
        ("tok", False, None),  # token present but unresolved, fall to default
        (None, False, None),  # no token, the configured default
    ],
)
def test_caller_principal_resolves_token_then_default(
    monkeypatch: pytest.MonkeyPatch,
    token_env: str | None,
    token_resolves: bool,
    expected: uuid.UUID | None,
) -> None:
    """caller_principal prefers a resolving bearer token, then falls to the configured default."""
    monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)
    if token_env is not None:
        monkeypatch.setenv(AUTH_TOKEN_ENV, token_env)
    monkeypatch.setattr(
        principal_module,
        "principal_for_token",
        _const(TOKEN_PRINCIPAL if token_resolves else None),
    )
    resolved = asyncio.run(caller_principal())
    assert resolved == (expected if expected is not None else settings.principal)


@pytest.mark.parametrize(
    ("env_token", "header", "expected"),
    [
        ("env-tok", {}, "env-tok"),  # the environment token takes precedence over any header
        (None, {"authorization": "Bearer hdr-tok"}, "hdr-tok"),  # http header bearer scheme
        (None, {"authorization": "Basic nope"}, None),  # a non-bearer scheme carries no token
        (None, {}, None),  # no source at all
    ],
)
def test_bearer_token_reads_env_then_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
    env_token: str | None,
    header: dict[str, str],
    expected: str | None,
) -> None:
    """bearer_token prefers the stdio env var and falls back to the http bearer header."""
    monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)
    if env_token is not None:
        monkeypatch.setenv(AUTH_TOKEN_ENV, env_token)
    monkeypatch.setattr(principal_module, "get_http_headers", lambda: header)
    assert bearer_token() == expected


@given(standing=st.booleans())
def test_require_admin_passes_an_admin_and_refuses_a_non_admin(
    monkeypatch: pytest.MonkeyPatch, standing: bool
) -> None:
    """require_admin returns the acting principal for an admin and raises ToolError otherwise."""
    monkeypatch.setattr(principal_module.Principal, "administers", _const(standing))
    if standing:
        assert asyncio.run(require_admin()) == settings.principal
    else:
        with pytest.raises(ToolError, match="admin principal"):
            asyncio.run(require_admin())


# section ordering the renderer guarantees, the lane to the header it leads its block with, in the
# widening-focus priority the agent reads top to bottom
SECTION_HEADERS = [
    ("profile", "profile:"),
    ("raptor", "overview:"),
    ("communities", "communities:"),
    ("facts", "facts:"),
    ("session", "working memory:"),
    ("hits", "sources:"),
]


@given(result=recall_results())
def test_render_orders_sections_and_includes_each_present_lane(
    result: RecallResult,
) -> None:
    """An empty bundle is one no-memory line, otherwise the present lanes lead in fixed order."""
    rendered = result.render()
    present = [(lane, header) for lane, header in SECTION_HEADERS if getattr(result, lane)]
    if not present:
        assert rendered == f"no memory recalled for {result.query!r}"
        return
    for _, header in present:
        assert header in rendered  # every populated lane contributes its section
    assert rendered.split("\n", 1)[0] == present[0][1]  # highest-priority lane leads


def test_render_empty_bundle_is_a_single_line() -> None:
    """A bundle with no lane populated renders the no-memory line, never a blank section."""
    result = RecallResult(
        query="nothing here", hits=[], facts=[], communities=[], raptor=[], as_of=None
    )
    assert result.render() == "no memory recalled for 'nothing here'"


def test_render_snapshot(snapshot: object) -> None:
    """A fully populated bundle pins the exact section layout, headers, and snippet trimming."""
    result = RecallResult(
        query="leech lattice",
        profile="Leech lattice: the optimal 24-dimensional sphere packing.",
        raptor=[RaptorNote(label="lattices", summary="packings and codes", level=2, score=0.9)],
        communities=[
            CommunityNote(label="coding theory", summary="dense lattices and codes", score=0.8)
        ],
        facts=[
            FactHit(
                statement="The Leech lattice is optimal in dimension 24.",
                predicate="implements",
                score=0.7,
                valid_from=None,
                valid_to=None,
            )
        ],
        hits=[
            Hit(
                document_title="lattice note",
                source_uri="vault/leech.md",
                text="   The   Leech\n\nlattice    is    a   remarkable\tobject.   " * 12,
                score=0.654321,
            )
        ],
        as_of=None,
    )
    assert result.render() == snapshot


def test_remember_writes_the_session_tier_and_returns_the_item_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remember verb writes one working item and renders its id, the cheap front capture."""
    item_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def stub_remember(text: str, **kwargs: object) -> uuid.UUID:
        captured["text"] = text
        captured["kind"] = kwargs["kind"]
        return item_id

    monkeypatch.setattr(server_module.extract_ingest, "remember_session", stub_remember)
    remember = asyncio.run(server.get_tools())["remember"]
    result = remember.run({"text": "a decision", "scope": None, "kind": "note"})
    assert asyncio.run(result).structured_content == {"result": str(item_id)}
    assert captured == {"text": "a decision", "kind": "note"}


def test_get_context_packs_the_recalled_lanes_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The get_context verb passes the query and budget through and renders the packed lanes."""
    captured: dict[str, object] = {}

    async def stub_pack(query: str, **kwargs: object) -> str:
        captured["query"] = query
        captured["token_budget"] = kwargs["token_budget"]
        return "facts:\n- (because) a holds."

    monkeypatch.setattr(server_module.retrieval, "assemble_context_pack", stub_pack)
    get_context = asyncio.run(server.get_tools())["get_context"]
    rendered = asyncio.run(
        get_context.run({"query": "what holds", "scope": None, "token_budget": 128})
    )
    assert captured == {"query": "what holds", "token_budget": 128}
    assert "(because) a holds." in text_of(rendered)


def test_reference_records_the_uri_and_returns_the_document_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reference verb records the locator and renders the new document id."""
    document_id = uuid.uuid4()
    monkeypatch.setattr(server_module.extract_ingest, "record_reference", _const(document_id))
    reference = asyncio.run(server.get_tools())["reference"]
    result = asyncio.run(reference.run({"uri": "https://arxiv.org/abs/1", "scope": None}))
    assert result.structured_content == {"result": str(document_id)}


def test_recall_routes_a_query_through_retrieval_and_renders_the_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recall verb passes the query and k through to retrieval and renders the bundle."""
    captured: dict[str, object] = {}

    async def stub_recall(query: str, **kwargs: object) -> RecallResult:
        captured["query"] = query
        captured["k"] = kwargs["k"]
        return RecallResult(
            query=query,
            hits=[Hit(document_title=None, source_uri=None, text="some passage", score=0.5)],
            facts=[
                FactHit(
                    statement="a holds.",
                    predicate="because",
                    score=0.4,
                    valid_from=None,
                    valid_to=None,
                )
            ],
            communities=[],
            raptor=[],
            as_of=None,
        )

    monkeypatch.setattr(server_module.retrieval, "recall", stub_recall)
    recall = asyncio.run(server.get_tools())["recall"]
    rendered = asyncio.run(recall.run({"query": "what holds", "scope": None, "k": 3}))
    body = text_of(rendered)
    assert captured == {"query": "what holds", "k": 3}
    assert "(because) a holds." in body
    assert "some passage" in body


class Rendered:
    """A stand-in for a report whose `render()` is the tool's one text-producing seam.

    text: the fixed string `render()` returns, so a test asserts on the tool's output directly.
    """

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self) -> str:
        return self.text


def admin_body_cases() -> list[tuple[str, dict[str, object], dict[str, object], str]]:
    """Each admin tool with its delegated seam faked, the call args, and an expected substring.

    Drives every operational tool body as the root admin, faking only the sibling-package call it
    delegates to (or, for the identity and group tools, the `Group`/`Principal` classmethod they
    now call directly under the one shared `system_session()` the test replaces wholesale), so the
    test asserts the argv the tool builds and the string it renders, never the delegated
    subsystem's behavior.
    """
    principal = str(uuid.uuid4())
    document = str(uuid.uuid4())
    new_id = uuid.uuid4()
    report = SimpleNamespace(
        n=4, hit_at_k=0.5, ndcg_at_k=0.4, mrr=0.3, mean_judge=None, per_config={"base": 0.5}
    )
    status = SimpleNamespace(pending=1, running=2, failed=0, lag=3, last_run=None)
    roster = [SimpleNamespace(id=new_id, display_name="Alice")]
    writes = [
        SimpleNamespace(
            id=new_id,
            kind="note",
            owner_id=new_id,
            scope=None,
            promoted_from=None,
            title="a note",
        )
    ]
    return [
        ("force_rebuild", {"graph.build_graph": _const((3, 5))}, {}, "created 3 entities and 5"),
        ("force_decay", {"graph.decay": _const(7)}, {}, "archived 7 stale facts"),
        ("force_reembed", {"graph.reembed": _const(9)}, {}, "re-embedded 9 vectors"),
        ("force_raptor", {"graph.build_raptor": _const(4)}, {}, "built 4 raptor summaries"),
        ("bench", {"run_eval": _const(report)}, {}, "base: 0.5"),
        (
            "sweep",
            {"run_sweep": _const(Rendered("SWEEP"))},
            {"dims": "512,1024"},
            "SWEEP",
        ),
        (
            "scale",
            {"run_scale_benchmark": _const(Rendered("SCALE"))},
            {"sizes": "100,200"},
            "SCALE",
        ),
        (
            "ingest",
            {"extract_ingest.ingest_path": _const(2)},
            {"path": "notes"},
            "ingested 2 documents from",
        ),
        (
            "ingest_image",
            {"extract_ingest.ingest_image": _const(new_id)},
            {"path": "a.png"},
            str(new_id),
        ),
        (
            "promote",
            {"graph.promote": _const(6)},
            {"document": document, "to_scope": "team"},
            "promoted 6 rows into team",
        ),
        ("tasks_status", {"tasks_overview": _const(status)}, {}, "pending=1"),
        (
            "create_user",
            {"Principal.create": _const(SimpleNamespace(id=new_id))},
            {"name": "bob"},
            str(new_id),
        ),
        (
            "grant_admin",
            {},
            {"principal": principal},
            "is now an admin",
        ),
        (
            "create_group",
            {"Group.create": _const(SimpleNamespace(id=new_id))},
            {"name": "team"},
            str(new_id),
        ),
        ("list_principals", {"Principal.list_all": _const(roster)}, {}, f"{new_id} Alice"),
        ("audit", {"Principal.recent_writes": _const(writes)}, {}, "[note]"),
        (
            "export_scope",
            {
                "export.export_scope": _const(
                    SimpleNamespace(documents=1, chunks=2, entities=3, facts=4, path="dump.jsonl")
                )
            },
            {"path": "dump.jsonl"},
            "exported 1 documents, 2 chunks, 3 entities, 4 facts to dump.jsonl",
        ),
        (
            "remove_member",
            {"Group.named": _const(SimpleNamespace(remove_member=_const(None)))},
            {"principal": principal, "group": "team"},
            f"{principal} removed from 'team'",
        ),
        (
            "publish_group",
            {"Group.named": _const(SimpleNamespace(publish=_const(None)))},
            {"group": "team"},
            "'team' is now public",
        ),
        (
            "curate_group",
            {"Group.named": _const(SimpleNamespace(curate=_const(None)))},
            {"group": "team"},
            "'team' is now curated",
        ),
        (
            "delete_group",
            {"Group.named": _const(SimpleNamespace(delete=_const(None)))},
            {"group": "team"},
            "'team' deleted",
        ),
        (
            "list_groups",
            {"Group.list_all": _const([{"name": "team", "public": True, "members": 2}])},
            {},
            "team [public] members=2",
        ),
    ]


@pytest.mark.parametrize(
    ("tool_name", "patches", "kwargs", "expected"),
    admin_body_cases(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_admin_tool_bodies_render_their_delegated_result(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    patches: dict[str, object],
    kwargs: dict[str, object],
    expected: str,
) -> None:
    """Each admin tool builds the right call and renders its result, run as the root admin.

    `system_session` is replaced for every case, harmless for the tools that never open one, so
    the identity and group tools this file drives never touch a real database either.
    """
    monkeypatch.setattr(server_module, "system_session", fake_system_session)
    for path, fake in patches.items():
        module_name, _, attr = path.rpartition(".")
        target = getattr(server_module, module_name) if module_name else server_module
        monkeypatch.setattr(target, attr, fake)
    tool = asyncio.run(server.get_tools())[tool_name]
    result = asyncio.run(tool.run(kwargs))
    assert expected in text_of(result)


def test_grant_admin_refuses_an_unknown_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    """An id `session.get` cannot resolve fails fast with a plain ToolError, no silent no-op."""
    monkeypatch.setattr(
        server_module, "system_session", lambda: FakeSystemSession(FakeSession(get_result=None))
    )
    grant_admin = asyncio.run(server.get_tools())["grant_admin"]
    with pytest.raises(ToolError, match="no principal"):
        asyncio.run(grant_admin.run({"principal": str(uuid.uuid4())}))


def test_benchmark_guards_disabled_engine_and_unknown_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The benchmark tool refuses when the engine is off and rejects an unknown dataset name."""
    benchmark = asyncio.run(server.get_tools())["benchmark"]
    with pytest.raises(ToolError, match="benchmarks are off"):
        asyncio.run(benchmark.run({"name": "evermembench", "dataset_path": "x.jsonl"}))

    monkeypatch.setattr(server_module.benchmarks, "LOADERS", {"evermembench": lambda path: []})
    monkeypatch.setattr(server_module.benchmarks, "benchmark_gold", lambda rows: rows)
    monkeypatch.setattr(server_module, "run_sweep", _const(Rendered("OK")))
    monkeypatch.setattr(settings, "benchmarks_enabled", True)
    with pytest.raises(ValueError, match="unknown benchmark"):
        asyncio.run(benchmark.run({"name": "nope", "dataset_path": "x.jsonl"}))
    good = asyncio.run(benchmark.run({"name": "evermembench", "dataset_path": "x.jsonl"}))
    assert text_of(good) == "OK"


def test_add_member_resolves_the_group_then_joins_the_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_member resolves the named group then enrolls the principal under the given role."""
    principal = str(uuid.uuid4())
    joined: dict[str, object] = {}

    async def stub_add(session: object, who: uuid.UUID, role: str = "writer") -> None:
        joined["who"] = who
        joined["role"] = role

    monkeypatch.setattr(server_module, "system_session", fake_system_session)
    monkeypatch.setattr(server_module.Group, "named", _const(SimpleNamespace(add_member=stub_add)))
    add_member = asyncio.run(server.get_tools())["add_member"]
    result = asyncio.run(
        add_member.run({"principal": principal, "group": "team", "role": "reader"})
    )
    assert text_of(result) == f"{principal} added to 'team' as reader"
    assert joined == {"who": uuid.UUID(principal), "role": "reader"}

    async def missing(session: object, name: str) -> None:
        raise ScopeNotFoundError(f"no scope named {name!r}")

    monkeypatch.setattr(server_module.Group, "named", missing)
    with pytest.raises(ValueError, match="no scope named"):
        asyncio.run(add_member.run({"principal": principal, "group": "ghost"}))


def test_resolve_scope_returns_null_for_a_private_write() -> None:
    """A null scope name stays null, the private write that never touches the database."""
    assert asyncio.run(resolve_scope(None, uuid.uuid4())) is None


def test_resolve_scope_looks_up_a_visible_group_and_fails_on_an_unknown_one(
    requires_db: None,
) -> None:
    """A known scope name resolves to its group id, an unknown one fails fast under the caller."""

    async def probe() -> uuid.UUID | None:
        group_id = await create_group("scope-probe")
        try:
            resolved = await resolve_scope("scope-probe", settings.system_principal_id)
            assert resolved == group_id
            with pytest.raises(ScopeNotFoundError, match="no scope named"):
                await resolve_scope("absent-scope", settings.system_principal_id)
            return resolved
        finally:
            from sqlalchemy import text

            from aizk.store import async_session

            async with async_session()() as session, session.begin():
                await session.execute(text("DELETE FROM group_ WHERE id = :id"), {"id": group_id})

    assert asyncio.run(probe()) is not None


def curation_tool_cases() -> list[tuple[str, object, dict[str, object], str]]:
    """Each group-admin curation tool with its `Group` instance seam faked, args, and rendering.

    All three resolve and vet the group through `resolve_group_admin`, faked wholesale so the test
    asserts the string each renders over its delegated `Group` method call without a database,
    `reject` also driving `parse_fact_ids` with a real comma list.
    """
    fact = SimpleNamespace(id=uuid.uuid4(), owner_id=uuid.uuid4(), statement="a claim")
    return [
        (
            "pending",
            SimpleNamespace(pending_facts=_const([fact])),
            {"group": "team"},
            f"{fact.id} by {fact.owner_id}: a claim",
        ),
        (
            "approve",
            SimpleNamespace(approve_facts=_const(2)),
            {"group": "team", "facts": "all"},
            "approved 2 facts in 'team'",
        ),
        (
            "reject",
            SimpleNamespace(reject_facts=_const(1)),
            {"group": "team", "facts": f"{uuid.uuid4()}, {uuid.uuid4()}"},
            "rejected 1 facts in 'team'",
        ),
    ]


@pytest.mark.parametrize(
    ("tool_name", "fake_group", "kwargs", "expected"),
    curation_tool_cases(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_curation_tools_resolve_the_group_then_render_their_delegated_result(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    fake_group: object,
    kwargs: dict[str, object],
    expected: str,
) -> None:
    """Each curation verb resolves and vets the group, then renders its Group method's result."""
    monkeypatch.setattr(server_module, "system_session", fake_system_session)
    monkeypatch.setattr(server_module, "resolve_group_admin", _const(fake_group))
    tool = asyncio.run(server.get_tools())[tool_name]
    result = asyncio.run(tool.run(kwargs))
    assert expected in text_of(result)


def test_resolve_group_admin_returns_the_group_once_the_caller_is_vetted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller who clears the admin check gets the resolved group object back."""

    class FakeGroup:
        async def require_admin(self, session: object, principal_id: uuid.UUID) -> None:
            pass

    fake_group = FakeGroup()
    monkeypatch.setattr(server_module.Group, "named", _const(fake_group))
    assert asyncio.run(resolve_group_admin(object(), "team")) is fake_group


def test_resolve_group_admin_wraps_a_domain_refusal_as_a_plain_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-admin caller reads the plain ToolError, never the raw domain NotGroupAdminError."""

    class FakeGroup:
        async def require_admin(self, session: object, principal_id: uuid.UUID) -> None:
            raise NotGroupAdminError("not your group")

    monkeypatch.setattr(server_module.Group, "named", _const(FakeGroup()))
    with pytest.raises(ToolError, match="not your group"):
        asyncio.run(resolve_group_admin(object(), "team"))


def test_parse_fact_ids_parses_a_comma_list_and_ignores_stray_whitespace() -> None:
    """The id parser splits on commas, trims each token, and drops the empty trailing field."""
    first, second = uuid.uuid4(), uuid.uuid4()
    assert parse_fact_ids(f" {first}, {second} ,") == [first, second]
    assert parse_fact_ids("") == []


@pytest.mark.parametrize("anonymous", [True, False], ids=["anonymous", "identified"])
def test_require_identified_refuses_the_anonymous_caller_and_admits_a_keyed_one(
    monkeypatch: pytest.MonkeyPatch, anonymous: bool
) -> None:
    """A write verb refuses the shared anonymous principal but returns any identified one.

    The anonymous HTTP stranger owns no principal row, so a write it issued would only die later on
    a foreign key; refusing here turns that into a clear read-only message instead (the e2e-found
    anonymous read-only regression).
    """
    who = settings.anonymous_principal_id if anonymous else uuid.uuid4()
    monkeypatch.setattr(principal_module, "caller_principal", _const(who))
    if anonymous:
        with pytest.raises(ToolError, match="read-only"):
            asyncio.run(require_identified())
    else:
        assert asyncio.run(require_identified()) == who


def test_anonymous_rate_limit_passes_the_authenticated_and_throttles_the_stranger_past_the_burst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authenticated caller flows unthrottled while a stranger drains one shared burst bucket.

    Composing the token bucket only over anonymous tool calls, any identified principal passes
    through while the shared anonymous bucket, sized to a five-second burst, admits the stranger's
    first call then refuses the next once drained (the e2e-found rate-limit burst regression).
    """
    limit = AnonymousRateLimit(max_requests_per_second=0.2)  # capacity max(1, round(1.0)) == 1
    served: list[object] = []

    async def call_next(context: object) -> str:
        served.append(context)
        return "ok"

    monkeypatch.setattr(middleware_module, "caller_principal", _const(uuid.uuid4()))
    assert asyncio.run(limit.on_call_tool(object(), call_next)) == "ok"

    monkeypatch.setattr(
        middleware_module, "caller_principal", _const(settings.anonymous_principal_id)
    )
    assert asyncio.run(limit.on_call_tool(object(), call_next)) == "ok"  # the lone burst token
    with pytest.raises(RateLimitError, match="anonymous rate limit"):
        asyncio.run(limit.on_call_tool(object(), call_next))  # bucket drained, stranger refused


def test_aizkmcp_wires_a_verifier_and_the_anonymous_rate_limit_on_the_http_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a configured issuer and HTTP on, the server attaches its verifier and the rate limit.

    Drives both `__init__` branches the stdio single-user default skips: a resolving `verifier()`
    hands the auth provider to FastMCP, and the shared HTTP transport composes the anonymous rate
    limit onto the middleware stack.
    """
    monkeypatch.setattr(settings, "zitadel_issuer", "https://issuer.test/aizk")
    monkeypatch.setattr(settings, "zitadel_jwks_url", "https://issuer.test/jwks")
    monkeypatch.setattr(settings, "zitadel_introspect_url", "")
    monkeypatch.setattr(settings, "mcp_http", True)

    probe = AizkMCP("probe")

    assert probe.auth is not None
    assert any(isinstance(mw, AnonymousRateLimit) for mw in probe.middleware)
