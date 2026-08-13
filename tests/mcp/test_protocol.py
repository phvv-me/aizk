import dbutil
import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.context import Context
from mcp.shared.exceptions import MCPError
from mcp_probe import USER_TOOLS, build_server

from aizk.mcp.middleware import ModernProtocolOnly, client_label


def test_aizk_negotiates_the_current_protocol() -> None:
    async def drive() -> None:
        client = Client(
            build_server(name="aizk-modern"),
            mode="2026-07-28",
            client_info=mt.Implementation(name="modern-probe", version="1"),
        )
        async with client:
            assert client.protocol_version == "2026-07-28"
            assert {tool.name for tool in await client.list_tools()} == USER_TOOLS

    dbutil.run(drive())


def test_aizk_rejects_legacy_initialization() -> None:
    async def drive() -> None:
        client = Client(build_server(name="aizk-legacy"), mode="legacy")
        with pytest.raises(MCPError, match="AIZK requires MCP 2026-07-28") as caught:
            async with client:
                pass
        assert caught.value.error.code == mt.UNSUPPORTED_PROTOCOL_VERSION

    dbutil.run(drive())


def test_client_identity_comes_from_modern_request_metadata() -> None:
    server = FastMCP("identity-modern")
    server.add_middleware(ModernProtocolOnly())

    @server.tool
    async def identify(context: Context) -> str | None:
        """Return the client label AIZK records with writes."""
        return client_label(context)

    async def drive() -> None:
        client = Client(
            server,
            mode="2026-07-28",
            client_info=mt.Implementation(name="modern-probe", version="1"),
        )
        async with client:
            result = await client.call_tool("identify")
            assert result.data == "modern-probe/1"

    dbutil.run(drive())
