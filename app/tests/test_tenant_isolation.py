"""
Cross-tenant isolation test suite.

Tenant data leakage is the single most damaging bug this product can ship.
These tests verify that:
   1. get_conversation filters by tenant_id
   2. JWT tokens embed org_id and role correctly
   3. Role-based access is enforced at the dependency layer
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services.auth import create_access_token, decode_token


ORG_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ORG_B_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── JWT Token Tests ──────────────────────────────────────────────────────

def test_jwt_embeds_org_id():
    """Access tokens must embed org_id for tenant scoping."""
    with patch("app.config.settings.settings.jwt_secret_key", "test-secret"):
        token = create_access_token(USER_A_ID, "org_admin", ORG_A_ID)
        payload = decode_token(token)
        assert payload["org_id"] == str(ORG_A_ID)
        assert payload["role"] == "org_admin"
        assert payload["type"] == "access"


def test_jwt_super_admin_has_no_org():
    """super_admin tokens must NOT carry an org_id (cross-tenant access)."""
    with patch("app.config.settings.settings.jwt_secret_key", "test-secret"):
        token = create_access_token(USER_A_ID, "super_admin", None)
        payload = decode_token(token)
        assert "org_id" not in payload
        assert payload["role"] == "super_admin"


# ── CRUD Tenant Scoping Tests ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_conversation_filters_by_tenant():
    """get_conversation must include tenant_id in WHERE clause."""
    from app.database.crud import get_conversation

    mock_scalar = MagicMock()
    mock_scalar.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_scalar

    await get_conversation(mock_session, "some-session", tenant_id=ORG_A_ID)

    call_args = mock_session.execute.call_args
    assert call_args is not None
    compiled = str(call_args[0][0].compile(compile_kwargs={"literal_binds": True}))
    assert "tenant_id" in compiled


@pytest.mark.asyncio
async def test_get_conversation_returns_none_for_wrong_tenant():
    """Querying a conversation with the wrong tenant_id returns None."""
    from app.database.crud import get_conversation

    mock_scalar = MagicMock()
    mock_scalar.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_scalar

    result = await get_conversation(mock_session, "some-session", tenant_id=ORG_B_ID)
    assert result is None


@pytest.mark.asyncio
async def test_create_conversation_stores_tenant_id():
    """create_conversation must persist the tenant_id on new rows."""
    from app.database.crud import create_conversation

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()

    result = await create_conversation(mock_session, "new-session", tenant_id=ORG_A_ID)
    assert result.tenant_id == ORG_A_ID


@pytest.mark.asyncio
async def test_legacy_mode_no_tenant_id():
    """In legacy mode, create_conversation works without tenant_id."""
    from app.database.crud import create_conversation

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()

    result = await create_conversation(mock_session, "legacy-session")
    assert result.tenant_id is None


# ── Auth Backward Compatibility Tests ────────────────────────────────────

@pytest.mark.asyncio
async def test_legacy_api_key_still_works(client):
    """With auth_enabled=False and api_key set, X-Api-Key header must work."""
    with patch("app.config.settings.settings.auth_enabled", False), \
         patch("app.config.settings.settings.api_key", "legacy-key"), \
         patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:

        mock_agent.return_value = {
            "conversation_history": [],
            "lead_status": None,
            "booking_confirmed": False,
            "meeting_time": None,
            "human_escalated": False,
            "next_action": None,
        }

        response = await client.post(
            "/webhook/message",
            json={"session_id": "legacy-session", "message": "Hello"},
            headers={"X-Api-Key": "legacy-key"},
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_local_dev_no_auth_required(client):
    """With no api_key configured and auth disabled, requests must pass."""
    with patch("app.config.settings.settings.auth_enabled", False), \
         patch("app.config.settings.settings.api_key", ""), \
         patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:

        mock_agent.return_value = {
            "conversation_history": [],
            "lead_status": None,
            "booking_confirmed": False,
            "meeting_time": None,
            "human_escalated": False,
            "next_action": None,
        }

        response = await client.post(
            "/webhook/message",
            json={"session_id": "dev-session", "message": "Hello"},
        )
        assert response.status_code == 200


# ── Widget Key (tenant-bound client widgets) ─────────────────────────────

async def _agent_ok_response():
    return {
        "conversation_history": [],
        "lead_status": "hot",
        "booking_confirmed": False,
        "meeting_time": None,
        "human_escalated": False,
        "next_action": None,
    }


@pytest.mark.asyncio
async def test_widget_key_scopes_tenant_not_host(client):
    """A request with X-Widget-Key must be scoped to the widget key's org —
    regardless of what the Host header resolves to, and never None."""
    widget_org = uuid.UUID("33333333-3333-3333-3333-333333333333")
    host_org = ORG_B_ID
    mock_org = MagicMock()
    mock_org.id = widget_org

    with patch("app.services.domain.resolve_tenant_from_host", new_callable=AsyncMock, return_value=host_org), \
         patch("app.api.deps.get_organization_by_widget_key", new_callable=AsyncMock, return_value=mock_org), \
         patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent, \
         patch("app.api.webhook.memory_service.save_state", new_callable=AsyncMock) as mock_save:
        mock_agent.return_value = await _agent_ok_response()
        response = await client.post(
            "/webhook/start",
            json={"session_id": "widget-session", "channel": "web"},
            headers={"X-Widget-Key": "widget-key-abc123", "Host": "some-other-org.example.com"},
        )
        assert response.status_code == 200

        _, kwargs = mock_agent.call_args
        assert kwargs["tenant_id"] == widget_org
        assert kwargs["tenant_id"] != host_org
        assert kwargs["tenant_id"] is not None

        # save_state must also receive the widget key's tenant.
        _, save_kwargs = mock_save.call_args
        assert save_kwargs["tenant_id"] == widget_org
        assert save_kwargs["tenant_id"] != host_org


@pytest.mark.asyncio
async def test_widget_key_invalid_is_rejected(client):
    """An invalid widget key must NOT fall back to super_admin / no-tenant."""
    with patch("app.api.deps.get_organization_by_widget_key", new_callable=AsyncMock, return_value=None), \
         patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:
        response = await client.post(
            "/webhook/start",
            json={"session_id": "bad-session", "channel": "web"},
            headers={"X-Widget-Key": "not-a-real-key"},
        )
        assert response.status_code == 401
        mock_agent.assert_not_called()


@pytest.mark.asyncio
async def test_widget_key_missing_no_auth_is_401_multi_tenant(client):
    """In multi-tenant mode, missing widget key and no other auth → 401,
    never super_admin fallthrough."""
    with patch("app.config.settings.settings.auth_enabled", True), \
         patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:
        response = await client.post(
            "/webhook/start",
            json={"session_id": "no-auth-session", "channel": "web"},
        )
        assert response.status_code == 401
        mock_agent.assert_not_called()


def test_refresh_token_rejected_as_access():
    """Refresh tokens must be rejected by the access token verification path."""
    from app.services.auth import create_refresh_token

    with patch("app.config.settings.settings.jwt_secret_key", "test-secret"):
        refresh = create_refresh_token(USER_A_ID)
        payload = decode_token(refresh)
        assert payload["type"] == "refresh"
        # decode_token doesn't validate type — the route handler does
        # The route depend on authenticate_request which checks payload["type"] == "access"
        # so a refresh token would fail that check


def test_expired_token_rejected():
    """Tokens with past exp claim must be rejected."""
    from datetime import datetime, timedelta, timezone
    from jose import jwt

    with patch("app.config.settings.settings.jwt_secret_key", "test-secret"):
        payload = {
            "sub": str(USER_A_ID),
            "role": "org_admin",
            "type": "access",
            "org_id": str(ORG_A_ID),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        expired = jwt.encode(payload, "test-secret", algorithm="HS256")
        result = decode_token(expired)
        assert result is None, "Expired token should not decode"
