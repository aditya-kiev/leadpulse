from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.config.settings import settings
from app.services.auth import (
    check_jwt_secret_configured,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --- Unit tests for auth service ---

class TestAuthService:
    def test_hash_and_verify_password(self):
        pw = "my-secure-password"
        hashed = hash_password(pw)
        assert hashed != pw
        assert verify_password(pw, hashed)
        assert not verify_password("wrong-password", hashed)

    def test_create_access_token_contains_correct_payload(self):
        uid = uuid4()
        role = "org_admin"
        org_id = uuid4()
        token = create_access_token(uid, role, org_id)
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == str(uid)
        assert payload["role"] == role
        assert payload["org_id"] == str(org_id)
        assert payload["type"] == "access"
        assert "exp" in payload

    def test_access_token_without_org(self):
        uid = uuid4()
        token = create_access_token(uid, "super_admin")
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == str(uid)
        assert payload["role"] == "super_admin"
        assert "org_id" not in payload

    def test_refresh_token_is_separate_type(self):
        uid = uuid4()
        token = create_refresh_token(uid)
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == str(uid)
        assert payload["type"] == "refresh"
        assert "role" not in payload

    def test_invalid_token_returns_none(self):
        assert decode_token("invalid.token.here") is None
        assert decode_token("") is None

    def test_expired_token_returns_none(self):
        uid = uuid4()
        with patch("app.services.auth.settings.jwt_access_token_ttl_minutes", -1):
            token = create_access_token(uid, "agent")
        payload = decode_token(token)
        assert payload is None


# --- Integration tests for auth API ---

class TestAuthAPI:
    @pytest.mark.asyncio
    async def test_login_success(self, client):
        """POST /auth/login with valid credentials returns tokens."""
        uid = uuid4()
        org_id = uuid4()
        password_hash = hash_password("correct-password")

        mock_user = MagicMock(spec=[
            "id", "email", "password_hash", "role", "organization_id", "is_active"
        ])
        mock_user.id = uid
        mock_user.email = "admin@test.com"
        mock_user.password_hash = password_hash
        mock_user.role = "org_admin"
        mock_user.organization_id = org_id
        mock_user.is_active = True

        with patch("app.config.settings.settings.auth_enabled", True):
            with patch("app.api.auth.get_user_by_email", new_callable=AsyncMock, return_value=mock_user):
                response = await client.post("/auth/login", json={
                    "email": "admin@test.com",
                    "password": "correct-password",
                })
                assert response.status_code == 200
                data = response.json()
                assert "access_token" in data
                assert "refresh_token" in data
                assert data["token_type"] == "bearer"
                assert data["role"] == "org_admin"
                assert data["user_id"] == str(uid)

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, client):
        """POST /auth/login with wrong password returns 401."""
        password_hash = hash_password("correct-password")

        mock_user = MagicMock(spec=["password_hash", "is_active"])
        mock_user.password_hash = password_hash
        mock_user.is_active = True

        with patch("app.config.settings.settings.auth_enabled", True):
            with patch("app.api.auth.get_user_by_email", new_callable=AsyncMock, return_value=mock_user):
                response = await client.post("/auth/login", json={
                    "email": "admin@test.com",
                    "password": "wrong-password",
                })
                assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_login_disabled_user(self, client):
        """POST /auth/login for disabled account returns 403."""
        password_hash = hash_password("correct-password")

        mock_user = MagicMock(spec=["password_hash", "is_active"])
        mock_user.password_hash = password_hash
        mock_user.is_active = False

        with patch("app.config.settings.settings.auth_enabled", True):
            with patch("app.api.auth.get_user_by_email", new_callable=AsyncMock, return_value=mock_user):
                response = await client.post("/auth/login", json={
                    "email": "disabled@test.com",
                    "password": "correct-password",
                })
                assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_refresh_token_works(self, client):
        """POST /auth/refresh with valid refresh token returns a new access token."""
        uid = uuid4()
        refresh = create_refresh_token(uid)

        mock_user = MagicMock(spec=["id", "is_active", "role", "organization_id"])
        mock_user.id = uid
        mock_user.is_active = True
        mock_user.role = "org_admin"
        mock_user.organization_id = uuid4()

        with patch("app.config.settings.settings.auth_enabled", True):
            with patch("app.api.auth.get_user_by_id", new_callable=AsyncMock, return_value=mock_user):
                response = await client.post("/auth/refresh", json={
                    "refresh_token": refresh,
                })
                assert response.status_code == 200
                data = response.json()
                assert "access_token" in data
                assert data["token_type"] == "bearer"
                payload = decode_token(data["access_token"])
                assert payload is not None
                assert payload["sub"] == str(uid)

    @pytest.mark.asyncio
    async def test_access_token_rejected_on_refresh_endpoint(self, client):
        """Using an access token (not refresh) on /auth/refresh returns 401."""
        uid = uuid4()
        access = create_access_token(uid, "org_admin")

        response = await client.post("/auth/refresh", json={
            "refresh_token": access,
        })
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_register_creates_user(self, client):
        """POST /auth/register creates a new org_admin user."""
        uid = uuid4()

        mock_user = MagicMock(spec=["id", "email", "role", "organization_id"])
        mock_user.id = uid
        mock_user.email = "new@test.com"
        mock_user.role = "org_admin"
        mock_user.organization_id = None

        with patch("app.config.settings.settings.auth_enabled", True):
            with patch("app.api.auth.get_user_by_email", new_callable=AsyncMock, return_value=None):
                with patch("app.api.auth.create_user", new_callable=AsyncMock, return_value=mock_user):
                    response = await client.post("/auth/register", json={
                        "email": "new@test.com",
                        "password": "secure-password",
                        "display_name": "New User",
                    })
                    assert response.status_code == 200
                    data = response.json()
                    assert data["email"] == "new@test.com"
                    assert data["role"] == "org_admin"

    @pytest.mark.asyncio
    async def test_register_duplicate_email(self, client):
        """POST /auth/register with existing email returns 409."""
        with patch("app.config.settings.settings.auth_enabled", True):
            with patch("app.api.auth.get_user_by_email", new_callable=AsyncMock, return_value=MagicMock()):
                response = await client.post("/auth/register", json={
                    "email": "existing@test.com",
                    "password": "secure-password",
                    "display_name": "Existing",
                })
                assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_me_endpoint_returns_user(self, client):
        """GET /auth/me returns the current user's profile."""
        uid = uuid4()
        org_id = uuid4()
        token = create_access_token(uid, "org_admin", org_id)

        mock_user = MagicMock(spec=[
            "id", "email", "display_name", "role", "organization_id", "is_active"
        ])
        mock_user.id = uid
        mock_user.email = "admin@test.com"
        mock_user.display_name = "Admin User"
        mock_user.role = "org_admin"
        mock_user.organization_id = org_id
        mock_user.is_active = True

        with patch("app.config.settings.settings.auth_enabled", True):
            # Both deps.py and auth.py import get_user_by_id from app.services.auth;
            # patch the reference in both modules so the auth flow works end-to-end
            with patch("app.api.deps.get_user_by_id", new_callable=AsyncMock, return_value=mock_user):
                with patch("app.api.auth.get_user_by_id", new_callable=AsyncMock, return_value=mock_user):
                    response = await client.get(
                        "/auth/me",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                assert response.status_code == 200
                data = response.json()
                assert data["email"] == "admin@test.com"
                assert data["role"] == "org_admin"
                assert data["organization_id"] == str(org_id)

    @pytest.mark.asyncio
    async def test_me_endpoint_no_auth_header(self, client):
        """GET /auth/me without Authorization header returns 401 when auth is enabled."""
        with patch("app.config.settings.settings.auth_enabled", True):
            response = await client.get("/auth/me")
            assert response.status_code == 401


# --- Tenant isolation tests ---

class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_crud_get_conversation_returns_none_for_wrong_tenant(self):
        """get_conversation with wrong tenant_id should return None."""
        from app.database.crud import get_conversation

        org_a_id = uuid4()
        session_id = "test-session-wrong-tenant"

        # Set up mock session.execute to return a result with no match
        mock_scalar = MagicMock()
        mock_scalar.scalar_one_or_none.return_value = None

        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_scalar)

        result = await get_conversation(mock_session, session_id, tenant_id=org_a_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_crud_get_conversation_filters_by_tenant(self):
        """get_conversation should include tenant_id in query when provided."""
        from app.database.crud import get_conversation

        org_id = uuid4()
        session_id = "test-session-tenant-filter"

        mock_lead = MagicMock()
        mock_lead.session_id = session_id
        mock_lead.tenant_id = org_id

        mock_scalar = MagicMock()
        mock_scalar.scalar_one_or_none.return_value = mock_lead

        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_scalar)

        result = await get_conversation(mock_session, session_id, tenant_id=org_id)
        assert result is not None
        assert result.tenant_id == org_id

    @pytest.mark.asyncio
    async def test_org_a_cannot_access_org_b_via_direct_conversation_get(self, client):
        """IDOR test: org A user cannot access org B's conversation via session_id."""
        org_a_id = uuid4()
        uid = uuid4()
        token = create_access_token(uid, "agent", org_a_id)

        mock_user = MagicMock(spec=["id", "role", "organization_id", "is_active"])
        mock_user.id = uid
        mock_user.role = "agent"
        mock_user.organization_id = org_a_id
        mock_user.is_active = True

        with patch("app.config.settings.settings.auth_enabled", True):
            with patch("app.api.deps.get_user_by_id", new_callable=AsyncMock, return_value=mock_user):
                with patch("app.api.conversation.get_conversation", new_callable=AsyncMock, return_value=None):
                    response = await client.get(
                        f"/conversation/test-session-org-b",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_webhook_master_key_still_works_when_auth_disabled(self, client):
        """Legacy mode: master API key works when auth is disabled."""
        with patch("app.config.settings.settings.auth_enabled", False):
            with patch("app.config.settings.settings.api_key", "master-key"):
                with patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:
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
                        json={"session_id": "test-session", "message": "Hello"},
                        headers={"X-API-Key": "master-key"},
                    )
                    assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_webhook_skips_auth_when_no_key_and_auth_disabled(self, client):
        """Legacy mode: no auth required when api_key is empty and auth is disabled."""
        with patch("app.config.settings.settings.auth_enabled", False):
            with patch("app.config.settings.settings.api_key", ""):
                with patch("app.api.webhook.run_agent", new_callable=AsyncMock) as mock_agent:
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
                        json={"session_id": "test-session", "message": "Hello"},
                    )
                    assert response.status_code == 200

    def test_jwt_secret_guard_raises_when_auth_enabled_without_secret(self):
        """AUTH_ENABLED=true with an empty JWT_SECRET_KEY must fail fast."""
        with patch("app.config.settings.settings.auth_enabled", True), \
             patch("app.config.settings.settings.jwt_secret_key", ""):
            with pytest.raises(RuntimeError, match="JWT_SECRET_KEY must be set"):
                check_jwt_secret_configured()

    def test_jwt_secret_guard_ok_when_auth_enabled_with_secret(self):
        """AUTH_ENABLED=true with a JWT_SECRET_KEY set must not raise."""
        with patch("app.config.settings.settings.auth_enabled", True), \
             patch("app.config.settings.settings.jwt_secret_key", "test-secret"):
            check_jwt_secret_configured()

    def test_jwt_secret_guard_ok_when_auth_disabled(self):
        """AUTH_ENABLED=false must never raise, regardless of JWT_SECRET_KEY."""
        with patch("app.config.settings.settings.auth_enabled", False), \
             patch("app.config.settings.settings.jwt_secret_key", ""):
            check_jwt_secret_configured()
        with patch("app.config.settings.settings.auth_enabled", False), \
             patch("app.config.settings.settings.jwt_secret_key", "test-secret"):
            check_jwt_secret_configured()

    def test_demo_token_secret_guard_raises_when_empty(self):
        """DEMO_TOKEN_SECRET empty must fail fast (demo endpoints always live)."""
        from app.main import check_demo_token_secret_configured
        with patch("app.config.settings.settings.demo_token_secret", ""):
            with pytest.raises(RuntimeError, match="DEMO_TOKEN_SECRET must be set"):
                check_demo_token_secret_configured()

    def test_demo_token_secret_guard_ok_when_set(self):
        """DEMO_TOKEN_SECRET set must not raise."""
        from app.main import check_demo_token_secret_configured
        with patch("app.config.settings.settings.demo_token_secret", "some-secret"):
            check_demo_token_secret_configured()

    @pytest.mark.asyncio
    async def test_create_conversation_stores_tenant_id(self):
        """create_conversation should store the tenant_id on the ORM object."""
        from app.database.crud import create_conversation

        org_id = uuid4()
        session_id = "new-session-tenant"

        mock_session = AsyncMock()

        result = await create_conversation(mock_session, session_id, tenant_id=org_id)
        assert result is not None
        assert result.tenant_id == org_id
        assert result.session_id == session_id


# --- REAL Postgres register tests (Task 3) ---

import os
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test",
)


@pytest.fixture
async def pg_session_factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
    finally:
        await engine.dispose()


class TestRegisterRealDB:
    async def _cleanup(self, factory, *, user_ids=None, org_ids=None):
        from app.database.models import Organization, User

        async with factory() as session:
            for uid in user_ids or []:
                await session.execute(delete(User).where(User.id == uid))
            for oid in org_ids or []:
                await session.execute(delete(Organization).where(Organization.id == oid))
            await session.commit()

    @pytest.mark.asyncio
    async def test_register_creates_organization_and_links_user(self, client, pg_session_factory):
        """REAL Postgres: register with organization_name must create an
        Organization row, link the user to it, and return its id.

        Before the fix, RegisterOut.organization_id was always None and no
        organization row was ever created — the dashboard had a user with no
        tenant."""
        from app.database.models import Organization, User

        user_id = org_id = None
        email = f"reg-{uuid4().hex[:8]}@test.local"
        org_name = f"Register Org {uuid4().hex[:6]}"
        try:
            with patch("app.config.settings.settings.auth_enabled", True):
                with patch("app.database.session.async_session_factory", pg_session_factory):
                    resp = await client.post("/auth/register", json={
                        "email": email,
                        "password": "secure-password-123",
                        "display_name": "Reg User",
                        "organization_name": org_name,
                    })
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["email"] == email
            assert data["role"] == "org_admin"
            assert data["organization_id"], "register must return the created organization id"

            org_id = data["organization_id"]
            user_id = data["user_id"]

            async with pg_session_factory() as session:
                org = (await session.execute(
                    select(Organization).where(Organization.id == org_id)
                )).scalar_one_or_none()
                assert org is not None, "an Organization row must exist in Postgres"
                assert org.name == org_name
                assert org.slug == org_name.lower().replace(" ", "-")
                assert org.billing_status == "trialing"

                user = (await session.execute(
                    select(User).where(User.id == user_id)
                )).scalar_one()
                assert str(user.organization_id) == org_id, "user must be linked to the org"
                assert user.role == "org_admin"
        finally:
            await self._cleanup(pg_session_factory, user_ids=[user_id], org_ids=[org_id])

    @pytest.mark.asyncio
    async def test_register_without_org_name_returns_none_org(self, client, pg_session_factory):
        """Backward compat: register with no organization_name still works and
        returns organization_id=None (no org created)."""
        from app.database.models import User

        user_id = None
        email = f"reg-norg-{uuid4().hex[:8]}@test.local"
        try:
            with patch("app.config.settings.settings.auth_enabled", True):
                with patch("app.database.session.async_session_factory", pg_session_factory):
                    resp = await client.post("/auth/register", json={
                        "email": email,
                        "password": "secure-password-123",
                        "display_name": "No Org User",
                    })
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["organization_id"] is None
            user_id = data["user_id"]
        finally:
            await self._cleanup(pg_session_factory, user_ids=[user_id])

    @pytest.mark.asyncio
    async def test_register_duplicate_org_name_gets_unique_slug(self, client, pg_session_factory):
        """Two registrations with the same org name must produce distinct slugs
        (the second gets a -2 suffix), never a unique-constraint crash."""
        from app.database.models import Organization, User

        user_ids = []
        org_ids = []
        email1 = f"reg-dup1-{uuid4().hex[:8]}@test.local"
        email2 = f"reg-dup2-{uuid4().hex[:8]}@test.local"
        org_name = f"Dup Org {uuid4().hex[:6]}"
        try:
            with patch("app.config.settings.settings.auth_enabled", True):
                with patch("app.database.session.async_session_factory", pg_session_factory):
                    r1 = await client.post("/auth/register", json={
                        "email": email1, "password": "p12345678", "display_name": "A",
                        "organization_name": org_name,
                    })
                    r2 = await client.post("/auth/register", json={
                        "email": email2, "password": "p12345678", "display_name": "B",
                        "organization_name": org_name,
                    })
            assert r1.status_code == 200, r1.text
            assert r2.status_code == 200, r2.text
            org_ids = [r1.json()["organization_id"], r2.json()["organization_id"]]
            user_ids = [r1.json()["user_id"], r2.json()["user_id"]]
            assert len(set(org_ids)) == 2, "two registrations must yield two distinct orgs"

            async with pg_session_factory() as session:
                slugs = [o.slug for o in (await session.execute(
                    select(Organization).where(Organization.id.in_(org_ids))
                )).scalars().all()]
            base = org_name.lower().replace(" ", "-")
            assert slugs[0] == base
            assert slugs[1] == f"{base}-2"
        finally:
            await self._cleanup(pg_session_factory, user_ids=user_ids, org_ids=org_ids)

