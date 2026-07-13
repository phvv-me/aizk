import uuid

import dbutil
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.context import Context
from fastmcp.tools import FunctionTool
from mcp_probe import USER_TOOLS, context_for
from pydantic import AnyHttpUrl

import aizk.mcp.server as server_module
from aizk.config import settings
from aizk.mcp.middleware import AnonymousRateLimit
from aizk.mcp.models import ShareResult, WriteResult
from aizk.mcp.server import AizkMCP, server
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")


def test_registration_is_exactly_the_client_verbs(tools: dict[str, FunctionTool]) -> None:
    assert set(tools) == USER_TOOLS


def test_server_requires_identity_context() -> None:
    with pytest.raises(ToolError, match="no user resolved"):
        dbutil.run(server.user(context_for()))


def test_init_wires_a_verifier_and_the_rate_limit_on_the_configured_http_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "logto_url", AnyHttpUrl("https://auth.test"))
    monkeypatch.setattr(settings, "mcp_public_url", AnyHttpUrl("https://aizk.test"))
    probe = AizkMCP("probe")

    assert isinstance(probe.auth, RemoteAuthProvider)
    assert any(isinstance(mw, AnonymousRateLimit) for mw in probe.middleware)


def test_recall_forwards_the_query_budget_and_resolved_user(
    monkeypatch: pytest.MonkeyPatch,
    as_caller: User,
    caller_context: Context,
    tools: dict[str, FunctionTool],
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    async def stub(query: str, user: User, token_budget: int | None = None) -> object:
        captured.update(query=query, token_budget=token_budget, user=user)
        return sentinel

    monkeypatch.setattr(server_module.retrieval, "recall", stub)
    out = dbutil.run(
        tools["recall"].fn(query="  what holds  ", budget=2000, context=caller_context)
    )
    assert out is sentinel
    assert captured == {
        "query": "what holds",
        "token_budget": 2000,
        "user": as_caller,
    }


@pytest.mark.parametrize("query", ["", " ", "\n\t"])
def test_recall_rejects_blank_queries_as_tool_errors(
    query: str, caller_context: Context, tools: dict[str, FunctionTool]
) -> None:
    with pytest.raises(ToolError, match="recall query cannot be blank"):
        dbutil.run(tools["recall"].fn(query=query, context=caller_context))


def test_remember_writes_under_the_identified_caller_and_returns_the_id(
    monkeypatch: pytest.MonkeyPatch,
    as_caller: User,
    caller_context: Context,
    tools: dict[str, FunctionTool],
) -> None:
    item_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def stub(user: User, text: str, **kwargs: object) -> uuid.UUID:
        captured.update(
            text=text,
            kind=kwargs["kind"],
            created_by=kwargs["created_by"],
            scopes=kwargs["scopes"],
        )
        return item_id

    monkeypatch.setattr(server_module.extract_ingest, "remember_session", stub)
    out = dbutil.run(tools["remember"].fn(text="a decision", kind="code", context=caller_context))
    assert out == WriteResult(id=item_id)
    assert captured == {
        "text": "a decision",
        "kind": "code",
        "created_by": as_caller.id,
        "scopes": frozenset({as_caller.id}),
    }


def test_reference_records_under_the_caller_and_returns_the_document_id(
    monkeypatch: pytest.MonkeyPatch,
    as_caller: User,
    caller_context: Context,
    tools: dict[str, FunctionTool],
) -> None:
    new_id = uuid.uuid4()
    captured: dict[str, str | uuid.UUID | tuple[uuid.UUID, ...]] = {}

    async def record(
        user: User, uri: str, *, created_by: uuid.UUID, scopes: tuple[uuid.UUID, ...]
    ) -> uuid.UUID:
        captured.update(uri=uri, created_by=created_by, scopes=scopes)
        return new_id

    monkeypatch.setattr(server_module.extract_ingest, "record_reference", record)

    result = dbutil.run(tools["reference"].fn(uri="https://example.test", context=caller_context))

    assert result == WriteResult(id=new_id)
    assert captured == {
        "uri": "https://example.test",
        "created_by": as_caller.id,
        "scopes": frozenset({as_caller.id}),
    }


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [("remember", {"text": "t"}), ("reference", {"uri": "u"})],
)
def test_write_verbs_refuse_the_anonymous_caller(
    tools: dict[str, FunctionTool],
    tool_name: str,
    kwargs: dict[str, object],
) -> None:
    anonymous = context_for(User.private(settings.anonymous_user_id))
    with pytest.raises(ToolError, match="anonymous"):
        dbutil.run(tools[tool_name].fn(context=anonymous, **kwargs))


def test_end_to_end_a_non_admin_client_lists_and_calls_the_whole_visible_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auto_setup", False)  # skip the queue-schema health probe

    async def drive() -> None:
        async with Client(server) as client:
            names = {t.name for t in await client.list_tools()}
            assert names == USER_TOOLS

    dbutil.run(drive())


def test_share_copies_documents_to_personal_scope_by_default(
    monkeypatch: pytest.MonkeyPatch,
    as_caller: User,
    caller_context: Context,
    tools: dict[str, FunctionTool],
) -> None:
    captured: dict[str, object] = {}

    async def stub_promote(
        document_ids: list[uuid.UUID], scopes: frozenset[uuid.UUID], user: User
    ) -> int:
        captured.update(document_ids=document_ids, scopes=scopes, user=user)
        return 3

    doc = uuid.uuid4()
    monkeypatch.setattr(server_module.graph, "promote", stub_promote)
    out = dbutil.run(tools["share"].fn(documents=[doc], context=caller_context))
    assert out == ShareResult(shared=3, scopes=())
    assert captured == {
        "document_ids": [doc],
        "scopes": frozenset({as_caller.id}),
        "user": as_caller,
    }


def test_share_resolves_an_explicit_organization_intersection(
    monkeypatch: pytest.MonkeyPatch, tools: dict[str, FunctionTool]
) -> None:
    organizations = frozenset({uuid.uuid4(), uuid.uuid4()})
    first, second = organizations
    caller = User.authorized(
        settings.default_user_id,
        read=organizations,
        write=(settings.default_user_id, *organizations),
        names={"A": first, "B": second},
    )
    captured: dict[str, object] = {}

    async def stub_promote(
        document_ids: list[uuid.UUID], scopes: frozenset[uuid.UUID], user: User
    ) -> int:
        captured.update(document_ids=document_ids, scopes=scopes)
        return 1

    monkeypatch.setattr(server_module.graph, "promote", stub_promote)
    document = uuid.uuid4()
    assert dbutil.run(
        tools["share"].fn(documents=[document], scopes=["A", "B"], context=context_for(caller))
    ) == ShareResult(shared=1, scopes=("A", "B"))
    assert captured == {"document_ids": [document], "scopes": organizations}
