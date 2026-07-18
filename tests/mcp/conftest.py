import mcp_probe
import pytest
from fastmcp.server.context import Context
from fastmcp.tools import FunctionTool
from mcp_probe import context_for

from aizk.config import settings
from aizk.store.identity import User


@pytest.fixture(scope="session")
def tools() -> dict[str, FunctionTool]:
    return mcp_probe.tools_of(mcp_probe.server)


@pytest.fixture
def as_caller() -> User:
    return User.private(settings.default_user_id)


@pytest.fixture
def caller_context(as_caller: User) -> Context:
    return context_for(as_caller)
