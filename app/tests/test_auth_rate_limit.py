"""Rate limiting for /auth/forgot-password (P0).

forgot-password is keyed by the target email (lowercased/trimmed) with a 1-hour
window (password_reset_rpm_limit) to stop mail-bombing. It uses the shared Redis
sliding-window limiter and falls back to a per-process in-memory window when
Redis is unavailable, so the endpoint is never fully unguarded.

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
from app.api.deps import reset_password_reset_rate_limits

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
    yield
    reset_password_reset_rate_limits()


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


def _auth_patches(pg_session_factory):
    return [
        patch("app.config.settings.settings.auth_enabled", True),
        patch("app.config.settings.settings.jwt_secret_key", "test-secret-key-for-reset"),
        patch("app.database.session.async_session_factory", pg_session_factory),
        patch("app.services.auth.settings.jwt_secret_key", "test-secret-key-for-reset"),
    ]


@pytest.mark.asyncio
async def test_forgot_password_4th_request_in_window_is_429(pg_session_factory, client):
    email = f"target-{uuid4().hex[:8]}@example.com"
    with ExitStack() as stack:
        for p in _auth_patches(pg_session_factory):
            stack.enter_context(p)
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
        for p in _auth_patches(pg_session_factory):
            stack.enter_context(p)
        stack.enter_context(patch("app.config.settings.settings.password_reset_rpm_limit", 1))
        assert (await client.post("/auth/forgot-password", json={"email": email_a})).status_code == 200
        assert (await client.post("/auth/forgot-password", json={"email": email_a})).status_code == 429
        assert (await client.post("/auth/forgot-password", json={"email": email_b})).status_code == 200


@pytest.mark.asyncio
async def test_forgot_password_resumes_after_window_passes(pg_session_factory, client, fake_redis):
    email = f"resume-{uuid4().hex[:8]}@example.com"
    with ExitStack() as stack:
        for p in _auth_patches(pg_session_factory):
            stack.enter_context(p)
        stack.enter_context(patch("app.config.settings.settings.password_reset_rpm_limit", 3))
        for _ in range(3):
            assert (await client.post("/auth/forgot-password", json={"email": email})).status_code == 200
        assert (await client.post("/auth/forgot-password", json={"email": email})).status_code == 429

        await fake_redis.flushall()

        assert (await client.post("/auth/forgot-password", json={"email": email})).status_code == 200


@pytest.mark.asyncio
async def test_forgot_password_redis_down_uses_inmem_fallback(pg_session_factory, client):
    email = f"fallback-{uuid4().hex[:8]}@example.com"
    with ExitStack() as stack:
        for p in _auth_patches(pg_session_factory):
            stack.enter_context(p)
        stack.enter_context(patch("app.config.settings.settings.password_reset_rpm_limit", 3))
        stack.enter_context(patch("app.services.redis.get_redis", return_value=None))
        for _ in range(3):
            assert (await client.post("/auth/forgot-password", json={"email": email})).status_code == 200
        r = await client.post("/auth/forgot-password", json={"email": email})
        assert r.status_code == 429
        assert "Retry-After" in r.headers
