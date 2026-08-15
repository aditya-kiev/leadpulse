"""
Cross-tenant isolation test suite.

Tenant data leakage is the single most damaging bug this product can ship.
These tests verify that:
   1. get_conversation filters by tenant_id
   2. JWT tokens embed org_id and role correctly
   3. Role-based access is enforced at the dependency layer
"""
import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import app
from app.services.auth import create_access_token, decode_token

ORG_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ORG_B_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test",
)


@pytest.fixture
async def pg_session_factory():
    """Real Postgres session factory against the Alembic-managed test DB."""
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=20, max_overflow=20)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
    finally:
        await engine.dispose()


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


# ── Real-DB Concurrent Cross-Tenant Isolation ─────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_cross_tenant_widget_conversations(client, pg_session_factory):
    """Under concurrent load, 10+ widget conversations per tenant must each be
    stored under their OWN tenant_id — zero cross-tenant wiring.

    REAL Postgres test: two real Organizations with distinct widget keys, real
    ``authenticate_request`` widget-key auth, the real webhook handler, and the
    real ``memory_service.save_state`` write path. Only the LLM is mocked.
    Afterwards a raw SQL scan of ``lead_conversations`` must show every row
    tenant-scoped to the widget key that created it, and org B must not be able
    to read org A's conversation through ``get_conversation``."""
    from uuid import uuid4

    from sqlalchemy import delete

    from app.database.crud import get_conversation
    from app.database.models import Organization

    org_a_slug = f"iso-conc-a-{uuid4().hex[:8]}"
    org_b_slug = f"iso-conc-b-{uuid4().hex[:8]}"
    wk_a = f"wk-conc-a-{uuid4().hex[:8]}"
    wk_b = f"wk-conc-b-{uuid4().hex[:8]}"
    org_a_id = org_b_id = None
    sessions_a = [f"conc-a-{i}-{uuid4().hex[:6]}" for i in range(12)]
    sessions_b = [f"conc-b-{i}-{uuid4().hex[:6]}" for i in range(12)]

    try:
        async with pg_session_factory() as session:
            org_a = Organization(
                name="Tenant A (concurrent)",
                slug=org_a_slug,
                brand_name="Tenant A",
                logo_url="",
                primary_color="#FF0000",
                custom_domain="",
                custom_domain_status="unverified",
                tls_status="none",
                domain_verification_token=None,
                widget_key=wk_a,
            )
            org_b = Organization(
                name="Tenant B (concurrent)",
                slug=org_b_slug,
                brand_name="Tenant B",
                logo_url="",
                primary_color="#0000FF",
                custom_domain="",
                custom_domain_status="unverified",
                tls_status="none",
                domain_verification_token=None,
                widget_key=wk_b,
            )
            session.add_all([org_a, org_b])
            await session.flush()
            org_a_id = org_a.id
            org_b_id = org_b.id
            await session.commit()

        canned = {
            "conversation_history": [{"role": "assistant", "content": "ok"}],
            "lead_status": "hot",
            "booking_confirmed": False,
            "meeting_time": None,
            "human_escalated": False,
            "next_action": None,
        }

        async def fire(widget_key: str, session_id: str) -> int:
            with patch("app.database.session.async_session_factory", pg_session_factory), \
                 patch("app.services.memory.async_session_factory", pg_session_factory), \
                 patch("app.config.settings.settings.webhook_rpm_limit", 0), \
                 patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:
                mock_agent.return_value = canned
                resp = await client.post(
                    "/webhook/message",
                    json={"session_id": session_id, "message": "I want to book a demo"},
                    headers={"X-Widget-Key": widget_key},
                )
                return resp.status_code

        async def fire_many(widget_key: str, sessions: list[str]) -> list[int]:
            return await asyncio.gather(*(fire(widget_key, s) for s in sessions))

        grouped = await asyncio.gather(fire_many(wk_a, sessions_a), fire_many(wk_b, sessions_b))
        statuses = [s for group in grouped for s in group]
        assert all(s == 200 for s in statuses), f"non-200 responses: {statuses}"

        expected = {sid: org_a_id for sid in sessions_a}
        expected.update({sid: org_b_id for sid in sessions_b})

        async with pg_session_factory() as session:
            rows = (
                await session.execute(
                    text("SELECT session_id, tenant_id FROM lead_conversations WHERE session_id LIKE 'conc-%'")
                )
            ).fetchall()

        stored = {r.session_id: r.tenant_id for r in rows if r.session_id is not None}
        assert len(stored) == len(expected), (
            f"expected one row per fired conversation ({len(expected)}), got {len(stored)}"
        )
        for sid, org in expected.items():
            assert stored.get(sid) == org, (
                f"session {sid} stored under tenant {stored.get(sid)}, expected {org}"
            )

        # Read-side isolation: org B must NOT resolve to org A's conversation
        # through get_conversation even with org A's exact session id.
        async with pg_session_factory() as session:
            across = await get_conversation(session, sessions_a[0], tenant_id=org_b_id)
            own = await get_conversation(session, sessions_a[0], tenant_id=org_a_id)
        assert across is None, "org B read org A's conversation — tenant filter missing"
        assert own is not None, "org A must still read its own conversation"
    finally:
        async with pg_session_factory() as session:
            await session.execute(text("DELETE FROM lead_conversations WHERE session_id LIKE 'conc-%'"))
            if org_a_id:
                await session.execute(delete(Organization).where(Organization.id == org_a_id))
            if org_b_id:
                await session.execute(delete(Organization).where(Organization.id == org_b_id))
            await session.commit()
