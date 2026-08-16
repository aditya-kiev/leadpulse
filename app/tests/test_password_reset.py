"""REAL Postgres tests for Task 2: password reset (P0).

Covers:
  - token creation / verification unit behavior
  - full forgot → reset flow against real Postgres
  - single-use revocation (replay of a consumed token is rejected)
  - expired token rejection
  - anti-enumeration (unknown email still returns 200)
  - email is sent with the reset link (stub email log)
"""

import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport
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


async def _seed_user(factory, *, email: str = None) -> dict:
    """Create a real user (org optional) and return ids for cleanup."""
    from app.database.models import User
    from app.services.auth import hash_password

    async with factory() as session:
        user = User(
            email=email or f"reset-{uuid4().hex[:8]}@test.local",
            password_hash=hash_password("old-password-123"),
            display_name="Reset User",
            role="org_admin",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        user_id = str(user.id)
        await session.commit()
        return {"user_id": user_id, "email": user.email}


async def _cleanup(factory, user_id: str):
    if not user_id:
        return
    from app.database.models import PasswordResetToken, User

    async with factory() as session:
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


# ── Unit: token create / verify ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_password_reset_token_is_single_use_jwt(pg_session_factory):
    from app.database.models import User
    from app.services.auth import (
        create_password_reset_token,
        decode_token,
        store_password_reset_token,
    )

    async with pg_session_factory() as session:
        user = User(email=f"tok-{uuid4().hex[:8]}@test.local", password_hash="x",
                    display_name="T", role="agent")
        session.add(user)
        await session.flush()
        token = create_password_reset_token(user)
        claims = decode_token(token)
        assert claims is not None
        assert claims["type"] == "password_reset"
        assert claims["sub"] == str(user.id)
        assert "jti" in claims
        assert "exp" in claims
        await session.rollback()


@pytest.mark.asyncio
async def test_verify_password_reset_token_rejects_used_and_unknown(pg_session_factory):
    from datetime import timedelta
    from app.database.models import User
    from app.services.auth import (
        create_password_reset_token,
        decode_token,
        get_password_reset_token,
        revoke_password_reset_token,
        store_password_reset_token,
        verify_password_reset_token,
    )

    user_id = None
    try:
        data = await _seed_user(pg_session_factory)
        user_id = data["user_id"]
        async with pg_session_factory() as session:
            from app.database.models import User as U
            user = (await session.execute(
                select(U).where(U.id == user_id)
            )).scalar_one()
            token = create_password_reset_token(user)
            claims = decode_token(token)
            jti = claims["jti"]
            await store_password_reset_token(
                session, jti=jti, user_id=user.id,
                expires_at=datetime.utcnow() + timedelta(minutes=30),
            )

            verified = await verify_password_reset_token(session, token)
            assert verified is not None
            assert str(verified[0].id) == user_id

            # Replay before revocation is fine; after revocation it must fail.
            await revoke_password_reset_token(session, jti)
            assert await verify_password_reset_token(session, token) is None

            # Unknown jti → None
            bogus = create_password_reset_token(user)
            bogus_claims = decode_token(bogus)
            bogus_claims["jti"] = "does-not-exist"
            from jose import jwt
            import uuid as _uuid
            bogus2 = jwt.encode({**bogus_claims, "jti": _uuid.uuid4().hex},
                                "test-secret", algorithm="HS256")
            from app.services.auth import verify_password_reset_token as vp
            assert await vp(session, bogus2) is None
    finally:
        await _cleanup(pg_session_factory, user_id)


# ── Real-PG API flow ───────────────────────────────────────────────────────

@pytest.fixture
async def client(pg_session_factory):
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_forgot_password_unknown_email_still_200(pg_session_factory, client):
    from app.config.settings import settings

    with patch("app.config.settings.settings.auth_enabled", True), \
         patch("app.config.settings.settings.jwt_secret_key", "test-secret-key-for-reset"), \
         patch("app.database.session.async_session_factory", pg_session_factory), \
         patch("app.services.auth.settings.jwt_secret_key", "test-secret-key-for-reset"):
        resp = await client.post("/auth/forgot-password", json={
            "email": "nobody@example.com",
        })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_full_forgot_and_reset_flow_real_db(pg_session_factory, client):
    """Real Postgres: forgot → email contains a usable token → reset works →
    old password no longer verifies, new one does."""
    from app.config.settings import settings
    from app.services.auth import verify_password, get_user_by_email
    from app.agent.tools.email import get_email_log

    data = await _seed_user(pg_session_factory)
    user_id = data["user_id"]

    email_before = len(get_email_log())
    try:
        with patch("app.config.settings.settings.auth_enabled", True), \
             patch("app.config.settings.settings.jwt_secret_key", "test-secret-key-for-reset"), \
             patch("app.config.settings.settings.password_reset_token_ttl_minutes", 30), \
             patch("app.config.settings.settings.resend_api_key", ""), \
             patch("app.config.settings.settings.app_hostname", "app.leadpulse.ai"), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.services.auth.settings.jwt_secret_key", "test-secret-key-for-reset"):
            resp = await client.post("/auth/forgot-password", json={"email": data["email"]})
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

        # Email must have been logged (stub sender) with a reset link.
        emails = get_email_log()[email_before:]
        assert emails, "forgot-password must enqueue a reset email"
        reset_email = emails[-1]
        assert reset_email["to"] == data["email"]
        assert "Reset your LeadPulse password" in reset_email["subject"]
        assert "auth/reset?token=" in reset_email["body"], reset_email["body"]
        token = reset_email["body"].split("auth/reset?token=", 1)[1].split()[0]
        assert token

        # Reset with the token.
        with patch("app.config.settings.settings.auth_enabled", True), \
             patch("app.config.settings.settings.jwt_secret_key", "test-secret-key-for-reset"), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.services.auth.settings.jwt_secret_key", "test-secret-key-for-reset"):
            resp = await client.post("/auth/reset-password", json={
                "token": token, "new_password": "brand-new-pass-456",
            })
            assert resp.status_code == 200, resp.text
            assert resp.json()["ok"] is True

        # Verify: old password fails, new password works.
        async with pg_session_factory() as session:
            user = await get_user_by_email(session, data["email"])
            assert user is not None
            assert verify_password("old-password-123", user.password_hash) is False
            assert verify_password("brand-new-pass-456", user.password_hash) is True

        # Replay the same token → must be rejected (single-use).
        with patch("app.config.settings.settings.auth_enabled", True), \
             patch("app.config.settings.settings.jwt_secret_key", "test-secret-key-for-reset"), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.services.auth.settings.jwt_secret_key", "test-secret-key-for-reset"):
            resp = await client.post("/auth/reset-password", json={
                "token": token, "new_password": "third-password",
            })
            assert resp.status_code == 400, resp.text
    finally:
        await _cleanup(pg_session_factory, user_id)


@pytest.mark.asyncio
async def test_reset_with_expired_token_rejected(pg_session_factory, client):
    """A token issued with an already-expired TTL must be rejected."""
    from app.config.settings import settings
    from app.services.auth import create_password_reset_token, decode_token
    from app.database.models import PasswordResetToken

    data = await _seed_user(pg_session_factory)
    user_id = data["user_id"]
    try:
        token = None
        async with pg_session_factory() as session:
            from app.database.models import User as U
            user = (await session.execute(
                select(U).where(U.id == user_id)
            )).scalar_one()
            token = create_password_reset_token(user)
            claims = decode_token(token)
            session.add(PasswordResetToken(
                user_id=user.id, jti=claims["jti"],
                expires_at=datetime.utcnow() - timedelta(minutes=1),
            ))
            await session.commit()

        with patch("app.config.settings.settings.auth_enabled", True), \
             patch("app.config.settings.settings.jwt_secret_key", "test-secret-key-for-reset"), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.services.auth.settings.jwt_secret_key", "test-secret-key-for-reset"):
            resp = await client.post("/auth/reset-password", json={
                "token": token, "new_password": "expired-pass",
            })
            assert resp.status_code == 400, resp.text
    finally:
        await _cleanup(pg_session_factory, user_id)


@pytest.mark.asyncio
async def test_reset_with_unknown_token_rejected(pg_session_factory, client):
    data = await _seed_user(pg_session_factory)
    user_id = data["user_id"]
    try:
        with patch("app.config.settings.settings.auth_enabled", True), \
             patch("app.config.settings.settings.jwt_secret_key", "test-secret-key-for-reset"), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.services.auth.settings.jwt_secret_key", "test-secret-key-for-reset"):
            resp = await client.post("/auth/reset-password", json={
                "token": "not-a-real-token", "new_password": "whatever-123",
            })
            assert resp.status_code == 400, resp.text
    finally:
        await _cleanup(pg_session_factory, user_id)


@pytest.mark.asyncio
async def test_forgot_password_email_failure_still_200(pg_session_factory, client):
    """If sending the email throws, forgot-password must still return 200
    (anti-enumeration + no partial failure visible to the caller)."""
    from app.config.settings import settings

    data = await _seed_user(pg_session_factory)
    user_id = data["user_id"]
    try:
        with patch("app.config.settings.settings.auth_enabled", True), \
             patch("app.config.settings.settings.jwt_secret_key", "test-secret-key-for-reset"), \
             patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.services.auth.settings.jwt_secret_key", "test-secret-key-for-reset"), \
             patch("app.services.notifications.send_password_reset_email",
                   new_callable=AsyncMock, side_effect=RuntimeError("smtp down")):
            resp = await client.post("/auth/forgot-password", json={"email": data["email"]})
            assert resp.status_code == 200, resp.text
            assert resp.json()["ok"] is True
    finally:
        await _cleanup(pg_session_factory, user_id)


# ── password_reset_url helper ──────────────────────────────────────────────

class TestPasswordResetUrl:
    def test_with_hostname(self):
        from app.config.settings import settings
        with patch.object(settings, "app_hostname", "app.leadpulse.ai"):
            from app.services.notifications import password_reset_url
            url = password_reset_url("tok123")
            assert url == "https://app.leadpulse.ai/auth/reset?token=tok123"

    def test_without_hostname_returns_token(self):
        from app.config.settings import settings
        with patch.object(settings, "app_hostname", ""):
            from app.services.notifications import password_reset_url
            assert password_reset_url("tok123") == "tok123"

    def test_with_scheme_preserved(self):
        from app.config.settings import settings
        with patch.object(settings, "app_hostname", "http://localhost:3000"):
            from app.services.notifications import password_reset_url
            assert password_reset_url("tok123") == "http://localhost:3000/auth/reset?token=tok123"
