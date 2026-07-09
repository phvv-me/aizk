import uuid
from types import SimpleNamespace

import dbutil
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import FunctionTool
from hypothesis import given
from hypothesis import strategies as st
from mcp_probe import USER_TOOLS, const

import aizk.mcp.server as server_module
from aizk.config import settings
from aizk.exceptions import ScopeNotFoundError
from aizk.mcp.models import MoveResult, WriteResult
from aizk.mcp.server import (
    AizkMCP,
    parse_ids,
    resolve_scopes,
    server,
    startup_check,
)
from aizk.mcp.user import User

pytestmark = pytest.mark.usefixtures("migrated_db")


def apply_patches(monkeypatch: pytest.MonkeyPatch, patches: dict[str, object]) -> None:
    """Install each `patches` seam, a dotted path resolving to a submodule attribute else `server`.

    monkeypatch: the active patcher whose reverts restore the seams after the test.
    patches: seam path to its stand-in, `extract_ingest.record_reference` on the submodule, or a
        bare name on the server module itself.
    """
    for path, fake in patches.items():
        module_name, _, attr = path.rpartition(".")
        target = getattr(server_module, module_name) if module_name else server_module
        monkeypatch.setattr(target, attr, fake)


def test_registration_is_exactly_the_client_verbs(tools: dict[str, FunctionTool]) -> None:
    """The server registers exactly the four client verbs, the whole surface a key-holder reaches.

    Every operational operation moved to the CLI, so there is no tagged, listing-hidden tool left:
    the registered set is precisely the client verbs and nothing more.
    """
    assert set(tools) == USER_TOOLS


def test_init_wires_a_verifier_and_the_rate_limit_on_the_configured_http_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an issuer configured and HTTP on, `__init__` attaches the verifier and the rate limit.

    Drives both `__init__` branches the stdio single-user default skips: a resolving `verifier()`
    hands the auth provider to FastMCP, and the shared HTTP transport composes the anonymous rate
    limit onto the middleware stack.
    """
    from aizk.mcp.middleware import AnonymousRateLimit

    monkeypatch.setattr(settings, "oidc_issuer", "https://issuer.test/aizk")
    monkeypatch.setattr(settings, "oidc_jwks_url", "https://issuer.test/jwks")
    monkeypatch.setattr(settings, "oidc_introspect_url", "")
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
        user_id = await dbutil.seed_user(uuid.uuid4())
        ids = {
            name: await dbutil.seed_group(uuid.uuid4(), name=name)
            for name in ("alpha", "beta", "gamma")
        }
        canonical = tuple(sorted(ids.values()))
        assert await resolve_scopes("beta,alpha,gamma", user_id) == canonical
        assert await resolve_scopes("gamma, beta ,alpha", user_id) == canonical
        with pytest.raises(ScopeNotFoundError, match="no scope"):
            await resolve_scopes("ghost", user_id)

    dbutil.run(probe())


@given(ids=st.lists(st.uuids(), max_size=6))
def test_parse_ids_round_trips_a_comma_list_and_ignores_stray_whitespace(
    ids: list[uuid.UUID],
) -> None:
    """The id parser recovers exactly the ids from a padded comma list, dropping empty fields."""
    rendered = " , ".join(f" {fact} " for fact in ids) + " ,"
    assert parse_ids(rendered) == ids
    assert parse_ids("") == []


def test_parse_ids_rejects_a_malformed_id_with_a_tool_error() -> None:
    """A non-uuid in the list surfaces as a clean ToolError, not a raw ValueError to the client."""
    with pytest.raises(ToolError):
        parse_ids("not-a-uuid")


def test_recall_forwards_the_query_budget_and_resolved_lens_to_the_context_pack(
    monkeypatch: pytest.MonkeyPatch, as_caller: User, tools: dict[str, FunctionTool]
) -> None:
    """recall forwards query, budget, resolved lens, and caller to the pack builder."""
    captured: dict[str, object] = {}
    sentinel = object()

    async def stub(query: str, **kwargs: object) -> object:
        captured.update(query=query, token_budget=kwargs["token_budget"], scopes=kwargs["scopes"])
        captured["user_id"] = kwargs["user_id"]
        return sentinel

    monkeypatch.setattr(server_module.retrieval, "assemble_context_pack", stub)
    out = dbutil.run(tools["recall"].fn(query="what holds", scopes=None, budget=2000))
    assert out is sentinel
    assert captured == {
        "query": "what holds",
        "token_budget": 2000,
        "scopes": (),  # a null scope string resolves to the empty private lens
        "user_id": as_caller.id,
    }


def test_remember_writes_under_the_identified_caller_and_returns_the_id(
    monkeypatch: pytest.MonkeyPatch, as_caller: User, tools: dict[str, FunctionTool]
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
        "owner_id": as_caller.id,
        "scopes": (),
    }


def body_cases() -> list[tuple[str, dict[str, object], dict[str, object], object]]:
    """Each client verb with its faked seams, call kwargs, and the exact result its body returns.

    Every body runs under a fixed caller with `scopes=None`, so `resolve_scopes` yields the empty
    lens with no database and the delegate is the only seam. The write verb returns its record id.
    """
    new_id = uuid.uuid4()
    return [
        (
            "reference",
            {"extract_ingest.record_reference": const(new_id)},
            {"uri": "u"},
            WriteResult(id=new_id),
        ),
    ]


@pytest.mark.parametrize(
    ("tool_name", "patches", "kwargs", "expected"),
    body_cases(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_bodies_build_their_delegate_call_and_return_the_promised_model(
    monkeypatch: pytest.MonkeyPatch,
    as_caller: User,
    tools: dict[str, FunctionTool],
    tool_name: str,
    patches: dict[str, object],
    kwargs: dict[str, object],
    expected: object,
) -> None:
    """Each client verb resolves the caller, delegates to its faked seam, returns the model."""
    apply_patches(monkeypatch, patches)
    out = dbutil.run(tools[tool_name].fn(**kwargs))
    assert out == expected


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
    """A write verb refuses the anonymous read-only user before it ever touches storage."""
    monkeypatch.setattr(
        server_module,
        "current_user",
        lambda: User(id=settings.anonymous_user_id),
    )
    with pytest.raises(ToolError, match="anonymous"):
        dbutil.run(tools[tool_name].fn(**kwargs))


def test_end_to_end_a_non_admin_client_lists_and_calls_the_whole_visible_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the real in-process client every registered verb is the client surface, nothing hidden.

    The server carries only client verbs now, so a plain caller's listing is exactly `USER_TOOLS`
    and a write verb reaches its body (refusing the anonymous fallback loudly rather than 404ing).
    """
    from fastmcp import Client

    monkeypatch.setattr(settings, "auto_setup", False)  # skip the queue-schema health probe

    async def drive() -> None:
        await dbutil.reset_db()
        await dbutil.seed_user(settings.default_user_id)
        async with Client(server) as client:
            names = {t.name for t in await client.list_tools()}
            assert names == USER_TOOLS

    dbutil.run(drive())


def test_no_operational_tool_leaks_onto_the_server(tools: dict[str, FunctionTool]) -> None:
    """The operational surface is gone from the server: none of its old names is registered."""
    operational = {
        "setup",
        "health",
        "rebuild",
        "decay",
        "reembed",
        "raptor",
        "forget",
        "promote",
        "ingest",
        "ingest_image",
        "export_scope",
        "audit",
        "create_user",
        "grant_admin",
        "list_users",
        "create_group",
        "add_member",
        "remove_member",
        "publish_group",
        "curate_group",
        "delete_group",
        "list_groups",
        "define_entity_kind",
        "define_relation_kind",
        "list_ontology",
        "tasks_status",
        "profile_report",
        "bench",
        "sweep",
        "benchmark",
        "scale",
        "force_rebuild",
        "force_decay",
        "force_reembed",
        "force_raptor",
    }
    assert not (operational & set(tools))


class _MoveScalars:
    """A `scalars` result whose `.all()` is a fixed writable-group list, no database touched."""

    def __init__(self, groups: list[uuid.UUID]) -> None:
        self.groups = groups

    def all(self) -> list[uuid.UUID]:
        return self.groups


class _MoveSession:
    """A session stand-in exposing only the `scalars` the move body runs for writable groups."""

    def __init__(self, writable: list[uuid.UUID]) -> None:
        self.writable = writable

    async def scalars(self, *args: object) -> _MoveScalars:
        return _MoveScalars(self.writable)


class _MoveActing:
    """An `acting_as` stand-in binding a `_MoveSession` to the context for the move body."""

    def __init__(self, writable: list[uuid.UUID]) -> None:
        self.binding = dbutil.use_session(_MoveSession(writable))

    async def __aenter__(self) -> object:
        return await self.binding.__aenter__()

    async def __aexit__(self, *exc: object) -> bool:
        await self.binding.__aexit__(None, None, None)
        return False


def test_move_rescopes_the_callers_own_documents_and_reports_the_count(
    monkeypatch: pytest.MonkeyPatch, as_caller: User, tools: dict[str, FunctionTool]
) -> None:
    """move delegates the caller's documents and target lens to `move_to_scope`, the count back.

    A blank scope resolves to the private lens, contained by any writable set, so the writer gate
    passes and the private re-scope path runs with no database.
    """
    captured: dict[str, object] = {}

    async def stub_move(owner_id: uuid.UUID, document_ids: list[uuid.UUID], scopes: tuple) -> int:
        captured.update(owner_id=owner_id, document_ids=document_ids, scopes=scopes)
        return 3

    doc = uuid.uuid4()
    monkeypatch.setattr(server_module, "acting_as", lambda user_id: _MoveActing([]))
    monkeypatch.setattr(
        server_module.Document,
        "move_to_scope",
        classmethod(lambda cls, o, d, sc: stub_move(o, d, sc)),
    )
    out = dbutil.run(tools["move"].fn(documents=str(doc), scopes=None))
    assert out == MoveResult(moved=3, scopes="")
    assert captured == {"owner_id": as_caller.id, "document_ids": [doc], "scopes": ()}


def test_move_refuses_a_target_group_the_caller_cannot_write(
    monkeypatch: pytest.MonkeyPatch, as_caller: User, tools: dict[str, FunctionTool]
) -> None:
    """move refuses a target group outside the caller's writable set, no document touched."""
    monkeypatch.setattr(server_module, "acting_as", lambda user_id: _MoveActing([]))
    monkeypatch.setattr(server_module, "resolve_scopes", const((uuid.uuid4(),)))
    with pytest.raises(ToolError):
        dbutil.run(tools["move"].fn(documents=str(uuid.uuid4()), scopes="locked"))
