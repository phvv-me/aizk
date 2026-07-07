import dbutil
import pytest
from fastmcp.tools.tool import FunctionTool

import aizk.mcp.server as server_module
from aizk.config import settings
from aizk.mcp.principal import Principal
from aizk.mcp.server import server


@pytest.fixture(scope="session")
def tools() -> dict[str, FunctionTool]:
    """The registered tool objects keyed by name, each carrying its `.fn` body and `.tags`."""
    return dbutil.run(server.get_tools())


@pytest.fixture
def as_caller(monkeypatch: pytest.MonkeyPatch) -> Principal:
    """Resolve every verb body's `current_principal` to a fixed caller, bypassing the auth seam."""
    caller = Principal(id=settings.principal)
    monkeypatch.setattr(server_module, "current_principal", lambda: caller)
    return caller
