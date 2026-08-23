"""Rate limiting for /auth/register, /auth/reset-password, /auth/refresh,
and /demo/token, plus a guard that /billing/webhook stays unlimited.

Follows the style of app/tests/test_auth_rate_limit.py: fakeredis-backed
sliding window, real Postgres through the ASGI client where the endpoint
touches the DB, 429 + Retry-After on trip, recovery after the window
(simulated by flushing Redis), and per-process in-memory fallback when
Redis is down.
"""

import os
from contextlib import ExitStack
from unittest.mock import patch
from uuid import uuid4

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

import app.services.redis as redis_mod
from app.api.deps import (
    reset_demo_mint_rate_limits,
    reset_refresh_rate_limits,
    reset_register_rate_limits,
    reset_reset_password_rate_limits,
)

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
    reset_register_rate_limits()
    reset_reset_password_rate_limits()
    reset_refresh_rate_limits()
    reset_demo_mint_rate_limits()
    yield
    reset_register_rate_limits()
    reset_reset_password_rate_limits()
    reset_refresh_rate_limits()
    reset_demo_mint_rate_limits()


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
        patch("app.config.settings.settings.jwt_secret_key", "test-secret-key-for-routes"),
        patch("app.database.session.async_session_factory", pg_session_factory),
        patch("app.services.auth.settings.jwt_secret_key", "test-secret-key-for-routes"),
    ]


def _enter_all(stack: ExitStack, patches) -> None:
    for p in patches:
        stack.enter_context(p)


async def _seed_user(factory):
    from app.database.models import User
    from app.services.auth import hash_password

    async with factory() as session:
        user = User(
            email=f"route-limit-{uuid4().hex[:8]}@test.local",
            password_hash=hash_password("pass-123-45"),
            display_name="Route Limit User",
            role="org_admin",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        data = {"user_id": str(user.id), "email": user.email}
        await session.commit()
        return data


async def _cleanup_user(factory, user_id: str):
    from sqlalchemy import delete

    from app.database.models import User

    async with factory() as session:
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


# ── /auth/register ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_trips_after_limit(pg_session_factory, client):
    """6th registration attempt within the hour (limit 5) is 429; allowed
    attempts return 409 (duplicate seeded email — no rows created)."""
    data = await _seed_user(pg_session_factory)
    try:
        with ExitStack() as stack:
            _enter_all(stack, _auth_patches(pg_session_factory))
            stack.enter_context(patch("app.config.settings.settings.register_rpm_limit", 5))
            payload = {
                "email": data["email"], "password": "x-pass-123", "display_name": "Dup"
            }
            for i in range(5):
                r = await client.post("/auth/register", json=payload)
                assert r.status_code == 409, f"attempt {i}: {r.text}"
            r = await client.post("/auth/register", json=payload)
            assert r.status_code == 429, r.text
            assert "Retry-After" in r.headers
    finally:
        await _cleanup_user(pg_session_factory, data["user_id"])


@pytest.mark.asyncio
async def test_register_resumes_after_window(pg_session_factory, client, fake_redis):
    data = await _seed_user(pg_session_factory)
    try:
        with ExitStack() as stack:
            _enter_all(stack, _auth_patches(pg_session_factory))
            stack.enter_context(patch("app.config.settings.settings.register_rpm_limit", 1))
            payload = {
                "email": data["email"], "password": "x-pass-123", "display_name": "Dup"
            }
            assert (await client.post("/auth/register", json=payload)).status_code == 409
            assert (await client.post("/auth/register", json=payload)).status_code == 429

            # Equivalent to the 1-hour window elapsing.
            await fake_redis.flushall()

            assert (await client.post("/auth/register", json=payload)).status_code == 409
    finally:
        await _cleanup_user(pg_session_factory, data["user_id"])


@pytest.mark.asyncio
async def test_register_redis_down_uses_inmem_fallback(pg_session_factory, client):
    data = await _seed_user(pg_session_factory)
    try:
        with ExitStack() as stack:
            _enter_all(stack, _auth_patches(pg_session_factory))
            stack.enter_context(patch("app.config.settings.settings.register_rpm_limit", 3))
            stack.enter_context(patch("app.services.redis.get_redis", return_value=None))
            payload = {
                "email": data["email"], "password": "x-pass-123", "display_name": "Dup"
            }
            for _ in range(3):
                assert (await client.post("/auth/register", json=payload)).status_code == 409
            r = await client.post("/auth/register", json=payload)
            assert r.status_code == 429
            assert "Retry-After" in r.headers
    finally:
        await _cleanup_user(pg_session_factory, data["user_id"])


# ── /auth/reset-password (token consumption) ────────────────────────────


@pytest.mark.asyncio
async def test_reset_password_token_guessing_is_capped(pg_session_factory, client):
    """An attacker brute-forcing reset tokens gets limited tries per hour per
    IP, not unlimited attempts against single-use tokens."""
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(
            patch("app.config.settings.settings.reset_password_rpm_limit", 10)
        )
        for i in range(10):
            r = await client.post("/auth/reset-password", json={
                "token": f"guessed-token-{i}", "new_password": "new-pass-123",
            })
            assert r.status_code == 400, f"attempt {i}: {r.text}"
        r = await client.post("/auth/reset-password", json={
            "token": "guessed-token-11", "new_password": "new-pass-123",
        })
        assert r.status_code == 429, r.text
        assert "Retry-After" in r.headers


@pytest.mark.asyncio
async def test_reset_password_resumes_after_window(pg_session_factory, client, fake_redis):
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(
            patch("app.config.settings.settings.reset_password_rpm_limit", 1)
        )
        payload = {"token": "bad-token", "new_password": "new-pass-123"}
        assert (await client.post("/auth/reset-password", json=payload)).status_code == 400
        assert (await client.post("/auth/reset-password", json=payload)).status_code == 429

        await fake_redis.flushall()

        assert (await client.post("/auth/reset-password", json=payload)).status_code == 400


@pytest.mark.asyncio
async def test_reset_password_redis_down_uses_inmem_fallback(pg_session_factory, client):
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(
            patch("app.config.settings.settings.reset_password_rpm_limit", 2)
        )
        stack.enter_context(patch("app.services.redis.get_redis", return_value=None))
        payload = {"token": "bad-token", "new_password": "new-pass-123"}
        for _ in range(2):
            assert (await client.post("/auth/reset-password", json=payload)).status_code == 400
        r = await client.post("/auth/reset-password", json=payload)
        assert r.status_code == 429
        assert "Retry-After" in r.headers


# ── /auth/refresh (keyed by verified token subject, IP fallback) ─────────


@pytest.mark.asyncio
async def test_refresh_per_subject_budget_is_shared_across_ips(pg_session_factory, client):
    """Two DIFFERENT IPs refreshing the SAME user share one subject bucket —
    an attacker rotating IPs with a stolen refresh token still exhausts it.
    (The ASGI test client has a fixed client host, so 'IP rotation' is
    simulated by the shared-subject assertion: both requests hit the same
    subject bucket and trip together.)"""
    data = await _seed_user(pg_session_factory)
    try:
        with ExitStack() as stack:
            _enter_all(stack, _auth_patches(pg_session_factory))
            stack.enter_context(patch("app.config.settings.settings.refresh_rpm_limit", 3))
            from app.services.auth import create_refresh_token

            token = create_refresh_token(data["user_id"])
            for i in range(3):
                r = await client.post("/auth/refresh", json={"refresh_token": token})
                assert r.status_code == 200, f"attempt {i}: {r.text}"
            r = await client.post("/auth/refresh", json={"refresh_token": token})
            assert r.status_code == 429, r.text
            assert "Retry-After" in r.headers
    finally:
        await _cleanup_user(pg_session_factory, data["user_id"])


@pytest.mark.asyncio
async def test_refresh_different_users_have_separate_buckets(pg_session_factory, client):
    """User B's refresh budget is unaffected by user A exhausting theirs."""
    data_a = await _seed_user(pg_session_factory)
    data_b = await _seed_user(pg_session_factory)
    try:
        with ExitStack() as stack:
            _enter_all(stack, _auth_patches(pg_session_factory))
            stack.enter_context(patch("app.config.settings.settings.refresh_rpm_limit", 1))
            from app.services.auth import create_refresh_token

            tok_a = create_refresh_token(data_a["user_id"])
            tok_b = create_refresh_token(data_b["user_id"])
            assert (await client.post("/auth/refresh", json={"refresh_token": tok_a})).status_code == 200
            assert (await client.post("/auth/refresh", json={"refresh_token": tok_a})).status_code == 429
            # Different subject → untouched bucket.
            assert (await client.post("/auth/refresh", json={"refresh_token": tok_b})).status_code == 200
    finally:
        await _cleanup_user(pg_session_factory, data_a["user_id"])
        await _cleanup_user(pg_session_factory, data_b["user_id"])


@pytest.mark.asyncio
async def test_refresh_garbage_tokens_fall_back_to_ip_key(pg_session_factory, client):
    """Undecodable tokens can't key by subject, so they share the per-IP
    bucket — junk-token floods are still limited."""
    with ExitStack() as stack:
        _enter_all(stack, _auth_patches(pg_session_factory))
        stack.enter_context(patch("app.config.settings.settings.refresh_rpm_limit", 2))
        for i in range(2):
            r = await client.post("/auth/refresh", json={"refresh_token": f"junk-{i}"})
            assert r.status_code == 401, f"attempt {i}: {r.text}"
        r = await client.post("/auth/refresh", json={"refresh_token": "junk-3"})
        assert r.status_code == 429, r.text
        assert "Retry-After" in r.headers


@pytest.mark.asyncio
async def test_refresh_resumes_after_window(pg_session_factory, client, fake_redis):
    data = await _seed_user(pg_session_factory)
    try:
        with ExitStack() as stack:
            _enter_all(stack, _auth_patches(pg_session_factory))
            stack.enter_context(patch("app.config.settings.settings.refresh_rpm_limit", 1))
            from app.services.auth import create_refresh_token

            token = create_refresh_token(data["user_id"])
            payload = {"refresh_token": token}
            assert (await client.post("/auth/refresh", json=payload)).status_code == 200
            assert (await client.post("/auth/refresh", json=payload)).status_code == 429

            await fake_redis.flushall()

            assert (await client.post("/auth/refresh", json=payload)).status_code == 200
    finally:
        await _cleanup_user(pg_session_factory, data["user_id"])


# ── /demo/token ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_demo_token_minting_is_capped(client):
    """Public unauthenticated endpoint: real demo usage works, but unlimited
    token minting is stopped."""
    with patch("app.config.settings.settings.demo_token_secret", "test-secret-32-chars-long!!"), \
         patch("app.config.settings.settings.demo_token_mint_rpm_limit", 3):
        for i in range(3):
            r = await client.post("/demo/token")
            assert r.status_code == 200, f"attempt {i}: {r.text}"
        r = await client.post("/demo/token")
        assert r.status_code == 429, r.text
        assert "Retry-After" in r.headers


@pytest.mark.asyncio
async def test_demo_token_resumes_after_window(client, fake_redis):
    with patch("app.config.settings.settings.demo_token_secret", "test-secret-32-chars-long!!"), \
         patch("app.config.settings.settings.demo_token_mint_rpm_limit", 1):
        assert (await client.post("/demo/token")).status_code == 200
        assert (await client.post("/demo/token")).status_code == 429

        await fake_redis.flushall()

        assert (await client.post("/demo/token")).status_code == 200


@pytest.mark.asyncio
async def test_demo_token_redis_down_uses_inmem_fallback(client):
    with patch("app.config.settings.settings.demo_token_secret", "test-secret-32-chars-long!!"), \
         patch("app.config.settings.settings.demo_token_mint_rpm_limit", 2), \
         patch("app.services.redis.get_redis", return_value=None):
        for i in range(2):
            assert (await client.post("/demo/token")).status_code == 200
        r = await client.post("/demo/token")
        assert r.status_code == 429
        assert "Retry-After" in r.headers


# ── /billing/webhook must stay UNLIMITED ─────────────────────────────────


@pytest.mark.asyncio
async def test_billing_webhook_is_never_rate_limited(client):
    """Stripe retries are legitimate and must never be dropped by a rate
    limiter; /billing/webhook relies on signature verification instead. A
    burst well past any plausible limit returns the normal non-429 error
    (404 while billing is disabled) every time."""
    with patch("app.config.settings.settings.stripe_enabled", False):
        for _ in range(15):
            r = await client.post(
                "/billing/webhook",
                content=b'{"fake": "event"}',
                headers={"Stripe-Signature": "t=1,v1=abc"},
            )
            assert r.status_code != 429, r.text
