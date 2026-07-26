"""
Tests for the Redis-backed sliding-window rate limiter.

Uses fakeredis (an in-process Redis simulator that speaks the Redis protocol
and supports Lua scripting via fakeredis[lua]), so no real Redis server is
needed.  The test exercises actual Lua script execution to catch real
script-loading bugs.
"""

import asyncio
from unittest.mock import patch

import fakeredis.aioredis
import pytest

import app.services.redis as redis_mod
from app.services.redis import RedisSlidingWindowRateLimiter


@pytest.fixture(autouse=True)
async def reset_redis_state():
    """Ensure a clean state before each test."""
    redis_mod._redis = None
    yield
    redis_mod._redis = None


@pytest.fixture
async def fake_redis():
    """Inject a fakeredis instance into app.services.redis."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis_mod._redis = fake
    with patch("app.services.redis.settings.redis_url", "redis://fake:6379"):
        yield fake
    redis_mod._redis = None


# ── Single-instance tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_acquire_allows_up_to_limit(fake_redis):
    limiter = RedisSlidingWindowRateLimiter()
    for i in range(5):
        wait = await limiter.acquire(rpm_limit=5, window=60)
        assert wait == 0, f"request {i} should be allowed, got wait={wait}"


@pytest.mark.asyncio
async def test_acquire_denies_once_limit_hit(fake_redis):
    limiter = RedisSlidingWindowRateLimiter()
    for _ in range(5):
        await limiter.acquire(rpm_limit=5, window=60)
    wait = await limiter.acquire(rpm_limit=5, window=60)
    assert wait > 0, "6th request should be denied"


@pytest.mark.asyncio
async def test_acquire_allows_after_window_slides(fake_redis):
    limiter = RedisSlidingWindowRateLimiter()
    for _ in range(5):
        await limiter.acquire(rpm_limit=5, window=1)
    await asyncio.sleep(1.1)
    wait = await limiter.acquire(rpm_limit=5, window=1)
    assert wait == 0, "Should allow after window slides"


# ── Cross-instance (cross-worker) test ───────────────────────────────────

@pytest.mark.asyncio
async def test_two_instances_share_rate_limit(fake_redis):
    """Two separate limiter instances (simulating separate worker processes)
    must enforce the COMBINED rate limit against the same Redis key."""
    limiter_a = RedisSlidingWindowRateLimiter()
    limiter_b = RedisSlidingWindowRateLimiter()

    for _ in range(3):
        wait = await limiter_a.acquire(rpm_limit=5, window=60)
        assert wait == 0

    for _ in range(2):
        wait = await limiter_b.acquire(rpm_limit=5, window=60)
        assert wait == 0

    wait_a = await limiter_a.acquire(rpm_limit=5, window=60)
    wait_b = await limiter_b.acquire(rpm_limit=5, window=60)
    assert wait_a > 0, "limiter_a denied after combined limit"
    assert wait_b > 0, "limiter_b denied after combined limit"


# ── Redis-unavailable fallback ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_when_redis_unavailable():
    redis_mod._redis = None
    with patch("app.services.redis.get_redis", return_value=None):
        limiter = RedisSlidingWindowRateLimiter()
        wait = await limiter.acquire(rpm_limit=5, window=60)
        assert wait == -1


# ── RPM=0 means disabled ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rpm_zero_returns_immediately(fake_redis):
    limiter = RedisSlidingWindowRateLimiter()
    wait = await limiter.acquire(rpm_limit=0, window=60)
    assert wait == 0


# ── Key stability assertion ──────────────────────────────────────────────

def test_key_prefix_is_stable():
    """The key prefix must not contain id(self) or anything process-unique."""
    a = RedisSlidingWindowRateLimiter()
    b = RedisSlidingWindowRateLimiter()
    assert a._key_prefix == b._key_prefix == "ratelimit:gemini"
