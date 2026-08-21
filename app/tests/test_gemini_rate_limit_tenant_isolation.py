"""FIX 1: the Gemini RPM limiter must be scoped per tenant.

_acquire_rate_limit() used a single global budget (redis key
``ratelimit:gemini:global`` / one process-wide deque), so in multi-tenant
deployments every tenant shared ONE Gemini budget: one tenant's burst of
leads throttled (or put to sleep) every other tenant's conversations.
Now each tenant id gets its own sliding window, and traffic without a
tenant context (platform key / legacy single-tenant) keeps the legacy
``ratelimit:gemini`` scope.
"""

import asyncio
from unittest.mock import patch
from uuid import uuid4

import fakeredis.aioredis
import pytest

import app.agent.gemini as gemini_mod
import app.services.redis as redis_mod


class _WouldBlock(AssertionError):
    """Raised by the sleep sentinel if the limiter ever has to wait."""

    def __init__(self, seconds: float):
        super().__init__(
            f"rate limiter had to wait {seconds}s — budgets are not independent"
        )
        self.seconds = seconds


async def _sleep_sentinel(seconds):
    raise _WouldBlock(seconds)


@pytest.fixture(autouse=True)
def _reset_gemini_limiter_state():
    def _reset():
        gemini_mod._redis_limiters = {}
        gemini_mod._sliding_timestamps = {}
        gemini_mod._sliding_lock = None
        redis_mod._redis = None

    _reset()
    yield
    _reset()


@pytest.fixture
async def fake_redis():
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis_mod._redis = fake
    with patch("app.config.settings.settings.redis_url", "redis://fake:6379"):
        yield fake
    redis_mod._redis = None


@pytest.mark.asyncio
async def test_two_tenants_have_independent_gemini_budgets(fake_redis):
    """Tenant A exhausting its budget must NOT throttle tenant B."""
    tenant_a = uuid4()
    tenant_b = uuid4()
    with patch("app.config.settings.settings.gemini_rpm_limit", 2), \
         patch("app.agent.gemini.asyncio.sleep", new=_sleep_sentinel):
        # Tenant A burns its full budget...
        await gemini_mod._acquire_rate_limit(tenant_a)
        await gemini_mod._acquire_rate_limit(tenant_a)
        # ...a third call inside the window MUST be throttled (would sleep).
        with pytest.raises(_WouldBlock):
            await gemini_mod._acquire_rate_limit(tenant_a)
        # Tenant B draws from its OWN budget — instant, no waiting.
        await gemini_mod._acquire_rate_limit(tenant_b)
        await gemini_mod._acquire_rate_limit(tenant_b)


@pytest.mark.asyncio
async def test_platform_scope_is_separate_from_tenant_scopes(fake_redis):
    """Anonymous/platform traffic keeps its own budget; a tenant's traffic
    must draw from the tenant's budget, not the platform's."""
    tenant = uuid4()
    with patch("app.config.settings.settings.gemini_rpm_limit", 2), \
         patch("app.agent.gemini.asyncio.sleep", new=_sleep_sentinel):
        await gemini_mod._acquire_rate_limit(None)
        await gemini_mod._acquire_rate_limit(None)
        with pytest.raises(_WouldBlock):
            await gemini_mod._acquire_rate_limit(None)
        # The tenant still has a FULL budget of its own.
        await gemini_mod._acquire_rate_limit(tenant)
        await gemini_mod._acquire_rate_limit(tenant)


@pytest.mark.asyncio
async def test_inmem_fallback_is_also_tenant_scoped():
    """Redis down: the in-memory fallback must scope deques per tenant too."""
    with patch("app.services.redis.get_redis", return_value=None), \
         patch("app.config.settings.settings.gemini_rpm_limit", 2), \
         patch("app.agent.gemini.asyncio.sleep", new=_sleep_sentinel):
        tenant_a = uuid4()
        tenant_b = uuid4()
        await gemini_mod._acquire_rate_limit(tenant_a)
        await gemini_mod._acquire_rate_limit(tenant_a)
        with pytest.raises(_WouldBlock):
            await gemini_mod._acquire_rate_limit(tenant_a)
        await gemini_mod._acquire_rate_limit(tenant_b)
        await gemini_mod._acquire_rate_limit(tenant_b)


@pytest.mark.asyncio
async def test_concurrent_burst_across_five_tenants_never_waits(fake_redis):
    """Audit repro: five simultaneous leads across five orgs must ALL be
    served instantly — zero cross-tenant serialization, zero sleeps."""
    tenants = [uuid4() for _ in range(5)]
    with patch("app.config.settings.settings.gemini_rpm_limit", 2), \
         patch("app.agent.gemini.asyncio.sleep", new=_sleep_sentinel):
        await asyncio.gather(
            *(gemini_mod._acquire_rate_limit(t) for t in tenants for _ in range(2))
        )
