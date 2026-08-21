import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["GEMINI_API_KEY"] = "test-fake-key"
os.environ["LANGSMITH_API_KEY"] = "ls-test-fake"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing"
os.environ["API_KEY"] = "test-master-api-key"
os.environ["DEMO_TOKEN_SECRET"] = "test-demo-token-secret-32-chars!!"

# Import AFTER the env overrides above: importing app.agent.graph pulls in
# app.config.settings, whose .env values must not win over the test env.
import app.agent.graph as graph_module


@pytest.fixture(autouse=True)
def mock_openai():
    mock_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.content = "This is a mock response from the AI assistant."
    mock_instance.ainvoke = AsyncMock(return_value=mock_response)

    with patch("langchain_google_genai.ChatGoogleGenerativeAI", return_value=mock_instance):
        yield


@pytest.fixture(autouse=True)
def mock_db_session():
    with patch("app.database.session.async_session_factory"):
        yield


@pytest.fixture(autouse=True)
def mock_settings_auth_disabled():
    """By default, run tests with auth_enabled=False for backward compat."""
    with patch("app.config.settings.settings.auth_enabled", False):
        yield


@pytest.fixture(autouse=True)
def reset_graph_module_caches():
    """Reset graph module-level caches around every test.

    ``get_graph`` caches the platform singleton in ``_agent_graph`` and
    per-tenant graphs in ``_tenant_graphs``/``_tenant_graph_keys``.  Without a
    reset, a graph built under one test's mocks leaks into later tests, and a
    tenant request can be served a stale platform graph built before tenant
    keys/configs existed.
    """

    def _reset():
        graph_module._agent_graph = None
        graph_module._tenant_graphs = {}
        graph_module._tenant_graph_keys = {}

    _reset()
    yield
    _reset()
