import dbutil
import pytest
from fastmcp.tools.tool import FunctionTool

import aizk.mcp.server as server_module
from aizk.config import settings
from aizk.mcp.principal import User
from aizk.mcp.server import server


@pytest.fixture(scope="session")
def tools() -> dict[str, FunctionTool]:
    """The registered tool objects keyed by name, each carrying its `.fn` body and `.tags`."""
    return dbutil.run(server.get_tools())


@pytest.fixture
def as_caller(monkeypatch: pytest.MonkeyPatch) -> User:
    """Resolve every verb body's `current_user` to a fixed caller, bypassing the auth seam."""
    caller = User(id=settings.default_user_id)
    monkeypatch.setattr(server_module, "current_user", lambda: caller)
    return caller
