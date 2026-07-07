import uuid
from collections.abc import Awaitable, Callable

from aizk.mcp.server import AizkMCP

# the memory verbs and curation tools every caller reaches, gated in-body rather than hidden from
# listing, versus the operational surface the admin tag carves apart; the two disjoint partitions
# the registration contract keeps apart.
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
    "tasks_status",
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


def text_of(result: object) -> str:
    """The rendered string a str-returning tool carries on its structured content.

    result: the `ToolResult` a `tool.run` resolved to.
    """
    content = getattr(result, "structured_content", None)
    assert isinstance(content, dict)
    return content["result"]


def const[T](value: T) -> Callable[..., Awaitable[T]]:
    """An async function ignoring its arguments and resolving to `value`, a seam stand-in.

    value: the constant the returned coroutine yields.
    """

    async def fixed(*args: object, **kwargs: object) -> T:
        return value

    return fixed


class Rendered:
    """A report stand-in whose `render()` is the one text-producing seam a tool body calls.

    text: the fixed string `render()` returns, so a test asserts on the tool's output directly.
    """

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self) -> str:
        return self.text


class FakeTarget:
    """A fetched-row stand-in for the one `session.get` an admin tool body still runs.

    id: the row id the `grant_admin` body reads and reports back.
    """

    def __init__(self, id_: uuid.UUID | None = None) -> None:
        self.id = id_ or uuid.uuid4()

    async def grant_admin(self, session: object) -> None:
        """No-op, the call the `grant_admin` tool body makes on the fetched row."""


class FakeSession:
    """A session stand-in exposing only `.get`, the one raw session call an admin tool body runs.

    get_result: the row `.get(Model, id)` resolves to, null included to drive the not-found branch.
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
