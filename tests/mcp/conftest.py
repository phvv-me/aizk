import dbutil
import pytest
from fastmcp.server.context import Context
from fastmcp.tools import FunctionTool
from mcp_probe import context_for

from aizk.config import settings
from aizk.mcp.server import server
from aizk.store.identity import User


@pytest.fixture(scope="session")
def tools() -> dict[str, FunctionTool]:
    registered = dbutil.run(server.list_tools())
    assert all(isinstance(tool, FunctionTool) for tool in registered)
    return {tool.name: tool for tool in registered if isinstance(tool, FunctionTool)}


@pytest.fixture
def as_caller() -> User:
    return User.private(settings.default_user_id)


@pytest.fixture
def caller_context(as_caller: User) -> Context:
    return context_for(as_caller)
