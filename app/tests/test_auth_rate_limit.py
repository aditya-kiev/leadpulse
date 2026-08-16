"""Rate limiting for /auth/forgot-password and /auth/login (P0).

forgot-password is keyed by the target email (lowercased/trimmed) with a 1-hour
window (password_reset_rpm_limit) to stop mail-bombing; login is keyed by the
(email, IP) pair with a 15-minute window (login_rpm_limit) to stop brute-forcing.
Both use the shared Redis sliding-window limiter and fall back to a per-process
in-memory window when Redis is unavailable, so the endpoints are never fully
unguarded.

Follows the rate-limiter style of app/tests/test_webhook_rate_limit.py
(fakeredis) and the auth-endpoint style of app/tests/test_password_reset.py
(real Postgres through the ASGI client).
"""

import os
from contextlib import ExitStack
from unittest.mock import patch
from uuid import uuid4

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

import app.services.redis as redis_mod
from app.api.deps import reset_login_rate_limits, reset_password_reset_rate_limits

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test",
)


@pytest.fixture
async def pg_session_factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_rate_limit_state():
    reset_password_reset_rate_limits()
    reset_login_rate_limits()
    yield
    reset_password_reset_rate_limits()
    reset_login_rate_limits()


@pytest.fixture
async def fake_redis():
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    prev = redis_mod._redis
    redis_mod._redis = fake
    with patch("app.services.redis.settings.redis_url", "redis://fake:6379"):
        yield fake
    redis_mod._redis = prev


@pytest.fixture
async def client(pg_session_factory, fake_redis):
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed_user(factory, *, email: str = None, password: str = "pass-123-45"):
    from app.database.models import User
    from app.services.auth import hash_password

    async with factory() as session:
        user = User(
            email=email or f"user-{uuid4().hex[:8]}@test.local",
            password_hash=hash_password(password),
            display_name="Rate User",
            role="org_admin",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        user_id = str(user.id)
        await session.commit()
        return {"user_id": user_id, "email": user.email, "password": password}


async def _cleanup(factory, user_id: str):
    if not user_id:
        return
    from sqlalchemy import delete

    from app.database.models import User

    async with factory() as session:
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


def _auth_patches(pg_session_factory):
    return [
        patch("app.config.settings.settings.auth_enabled", True),
        patch("app.config.settings.settings.jwt_secret_key", "test-secret-key-for-reset"),
        patch("app.database.session.async_session_factory", pg_session_factory),
        patch("app.services.auth.settings.jwt_secret_key", "test-secret-key-for-reset"),
    ]


def _enter_all(stack: ExitStack, patches) -> None:
    for p in patches:
        stack.enter_context(p)


# ── Task 1: /auth/forgot-password ────────────────────────────────────────

@pytest.mark.asyncio
async def test_forgot_password_4th_request_in_window_is_429(pg_session_factory, client):
    email = f"target-{uuid4().hex[:8]}@example.com"
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(patch("app.config.settings.settings.password_reset_rpm_limit", 3))
        for _ in range(3):
            r = await client.post("/auth/forgot-password", json={"email": email})
            assert r.status_code == 200, r.text
        r = await client.post("/auth/forgot-password", json={"email": email})
        assert r.status_code == 429, r.text
        assert "Retry-After" in r.headers
        assert r.json()["detail"] == "Too many requests. Please slow down."


@pytest.mark.asyncio
async def test_forgot_password_different_email_unaffected(pg_session_factory, client):
    email_a = f"a-{uuid4().hex[:8]}@example.com"
    email_b = f"b-{uuid4().hex[:8]}@example.com"
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(patch("app.config.settings.settings.password_reset_rpm_limit", 1))
        assert (await client.post("/auth/forgot-password", json={"email": email_a})).status_code == 200
        assert (await client.post("/auth/forgot-password", json={"email": email_a})).status_code == 429
        # A different email, even at the same time, is unaffected.
        assert (await client.post("/auth/forgot-password", json={"email": email_b})).status_code == 200


@pytest.mark.asyncio
async def test_forgot_password_resumes_after_window_passes(pg_session_factory, client, fake_redis):
    email = f"resume-{uuid4().hex[:8]}@example.com"
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(patch("app.config.settings.settings.password_reset_rpm_limit", 3))
        for _ in range(3):
            assert (await client.post("/auth/forgot-password", json={"email": email})).status_code == 200
        assert (await client.post("/auth/forgot-password", json={"email": email})).status_code == 429

        # Clear the rate-limit keys (equivalent to the 1-hour window elapsing).
        await fake_redis.flushall()

        assert (await client.post("/auth/forgot-password", json={"email": email})).status_code == 200


@pytest.mark.asyncio
async def test_forgot_password_redis_down_uses_inmem_fallback(pg_session_factory, client):
    email = f"fallback-{uuid4().hex[:8]}@example.com"
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(patch("app.config.settings.settings.password_reset_rpm_limit", 3))
        stack.enter_context(patch("app.services.redis.get_redis", return_value=None))
        for _ in range(3):
            assert (await client.post("/auth/forgot-password", json={"email": email})).status_code == 200
        r = await client.post("/auth/forgot-password", json={"email": email})
        assert r.status_code == 429
        assert "Retry-After" in r.headers


# ── Task 2: /auth/login ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_same_email_ip_blocked_in_window(pg_session_factory, client):
    email = f"bf-{uuid4().hex[:8]}@example.com"  # nonexistent → 401 on each allowed try
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(patch("app.config.settings.settings.login_rpm_limit", 3))
        for _ in range(3):
            r = await client.post("/auth/login", json={"email": email, "password": "wrong"})
            assert r.status_code == 401, r.text
        r = await client.post("/auth/login", json={"email": email, "password": "wrong"})
        assert r.status_code == 429, r.text
        assert "Retry-After" in r.headers


@pytest.mark.asyncio
async def test_login_different_email_on_same_ip_unaffected(pg_session_factory, client):
    email_a = f"bfa-{uuid4().hex[:8]}@example.com"
    email_b = f"bfb-{uuid4().hex[:8]}@example.com"
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(patch("app.config.settings.settings.login_rpm_limit", 1))
        # A is now exhausted for its (email, IP) key.
        assert (await client.post("/auth/login", json={"email": email_a, "password": "x"})).status_code == 401
        assert (await client.post("/auth/login", json={"email": email_a, "password": "x"})).status_code == 429
        # B on the SAME ip is unaffected.
        assert (await client.post("/auth/login", json={"email": email_b, "password": "x"})).status_code == 401


@pytest.mark.asyncio
async def test_login_success_does_not_reset_counter(pg_session_factory, client):
    data = await _seed_user(pg_session_factory)
    try:
        with ExitStack() as stack:
            _enter_all(stack, _auth_patches(pg_session_factory))
            stack.enter_context(patch("app.config.settings.settings.login_rpm_limit", 3))
            # Three successful logins consume all 3 slots; a 4th (also correct)
            # must still be blocked — success never resets the sliding window.
            for _ in range(3):
                r = await client.post("/auth/login", json={"email": data["email"], "password": data["password"]})
                assert r.status_code == 200, r.text
            r = await client.post("/auth/login", json={"email": data["email"], "password": data["password"]})
            assert r.status_code == 429, r.text
            assert "Retry-After" in r.headers
    finally:
        await _cleanup(pg_session_factory, data["user_id"])


@pytest.mark.asyncio
async def test_login_redis_down_uses_inmem_fallback(pg_session_factory, client):
    email = f"logfallback-{uuid4().hex[:8]}@example.com"
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(patch("app.config.settings.settings.login_rpm_limit", 3))
        stack.enter_context(patch("app.services.redis.get_redis", return_value=None))
        for _ in range(3):
            assert (await client.post("/auth/login", json={"email": email, "password": "x"})).status_code == 401
        r = await client.post("/auth/login", json={"email": email, "password": "x"})
        assert r.status_code == 429
