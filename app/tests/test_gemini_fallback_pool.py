"""Platform-fallback Gemini key traffic must pool into ONE shared bucket.

resolve_tenant_gemini_key() silently falls back to settings.gemini_api_key
when a tenant has no own ``gemini`` crm_configs key (or it fails to
decrypt). Those tenants all burn the SAME real Google quota, so giving each
its own private rate-limit bucket multiplied a single quota N ways. Now:

- fallback-key tenant traffic shares ONE bucket (gemini_platform_fallback_rpm_limit),
  separate from genuinely-isolated per-tenant buckets;
- a tenant with their OWN key stays fully isolated — from the fallback pool
  AND from other tenants (guarantee from test_gemini_rate_limit_tenant_isolation.py,
  which must keep passing unchanged).
"""

import asyncio
from unittest.mock import patch
from uuid import UUID, uuid4

import fakeredis.aioredis
import pytest

import app.agent.gemini as gemini_mod
import app.services.redis as redis_mod


class _WouldBlock(AssertionError):
    def __init__(self, seconds: float):
        super().__init__(f"rate limiter had to wait {seconds}s")
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


TENANT_A = uuid4()
TENANT_B = uuid4()


@pytest.mark.asyncio
async def test_two_fallback_tenants_pool_into_one_shared_budget(fake_redis):
    """Two tenants BOTH on the platform fallback key share one budget: when A
    exhausts it, B is throttled too. This is CORRECT behavior — they share
    one real Google quota."""
    with patch("app.config.settings.settings.gemini_platform_fallback_rpm_limit", 2), \
         patch("app.config.settings.settings.gemini_rpm_limit", 2), \
         patch("app.agent.gemini.asyncio.sleep", new=_sleep_sentinel):
        # Tenant A (on fallback) burns the SHARED budget...
        await gemini_mod._acquire_rate_limit(TENANT_A, using_platform_key=True)
        await gemini_mod._acquire_rate_limit(TENANT_A, using_platform_key=True)
        with pytest.raises(_WouldBlock):
            await gemini_mod._acquire_rate_limit(TENANT_A, using_platform_key=True)
        # ...so tenant B (ALSO on fallback) is throttled by A's usage.
        with pytest.raises(_WouldBlock):
            await gemini_mod._acquire_rate_limit(TENANT_B, using_platform_key=True)


@pytest.mark.asyncio
async def test_own_key_tenant_is_isolated_from_exhausted_fallback_pool(fake_redis):
    """A tenant with their OWN key draws from their private bucket even when
    the platform-fallback pool is completely exhausted."""
    with patch("app.config.settings.settings.gemini_platform_fallback_rpm_limit", 1), \
         patch("app.config.settings.settings.gemini_rpm_limit", 2), \
         patch("app.agent.gemini.asyncio.sleep", new=_sleep_sentinel):
        # Fallback pool exhausted.
        await gemini_mod._acquire_rate_limit(TENANT_A, using_platform_key=True)
        with pytest.raises(_WouldBlock):
            await gemini_mod._acquire_rate_limit(TENANT_B, using_platform_key=True)

        # Own-key tenant: fully independent, instant slots.
        await gemini_mod._acquire_rate_limit(TENANT_B, using_platform_key=False)
        await gemini_mod._acquire_rate_limit(TENANT_B, using_platform_key=False)
        with pytest.raises(_WouldBlock):
            await gemini_mod._acquire_rate_limit(TENANT_B, using_platform_key=False)


@pytest.mark.asyncio
async def test_fallback_pool_uses_its_own_setting_not_gemini_rpm_limit(fake_redis):
    """The pooled bucket is sized by gemini_platform_fallback_rpm_limit."""
    with patch("app.config.settings.settings.gemini_platform_fallback_rpm_limit", 3), \
         patch("app.config.settings.settings.gemini_rpm_limit", 1), \
         patch("app.agent.gemini.asyncio.sleep", new=_sleep_sentinel):
        # If the pool wrongly used gemini_rpm_limit=1 this second call would sleep.
        await gemini_mod._acquire_rate_limit(TENANT_A, using_platform_key=True)
        await gemini_mod._acquire_rate_limit(TENANT_B, using_platform_key=True)
        await gemini_mod._acquire_rate_limit(TENANT_A, using_platform_key=True)
        with pytest.raises(_WouldBlock):
            await gemini_mod._acquire_rate_limit(TENANT_B, using_platform_key=True)


@pytest.mark.asyncio
async def test_inmem_fallback_pools_fallback_tenants_too():
    """Redis down: the in-memory deque must pool fallback-key tenants as well."""
    with patch("app.services.redis.get_redis", return_value=None), \
         patch("app.config.settings.settings.gemini_platform_fallback_rpm_limit", 2), \
         patch("app.config.settings.settings.gemini_rpm_limit", 2), \
         patch("app.agent.gemini.asyncio.sleep", new=_sleep_sentinel):
        await gemini_mod._acquire_rate_limit(TENANT_A, using_platform_key=True)
        await gemini_mod._acquire_rate_limit(TENANT_A, using_platform_key=True)
        with pytest.raises(_WouldBlock):
            await gemini_mod._acquire_rate_limit(TENANT_B, using_platform_key=True)
        # Own-key tenant unaffected in in-memory mode too.
        await gemini_mod._acquire_rate_limit(TENANT_B, using_platform_key=False)


# ── build_graph threading ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_graph_stamps_using_platform_key_correctly():
    """build_graph derives using_platform_key by comparing the resolved key
    with settings.gemini_api_key — identical keys MUST share the pool even if
    they came from different 'sources'."""
    from app.agent import graph as graph_mod

    platform_key = "platform-shared-key"
    recorded = []

    def fake_retry_model(model, **kwargs):
        recorded.append(kwargs)
        return model

    with patch.object(graph_mod, "ChatGoogleGenerativeAI", lambda **kw: object()), \
         patch.object(graph_mod, "RetryingGeminiModel", side_effect=fake_retry_model), \
         patch("app.config.settings.settings.gemini_api_key", platform_key):
        # Tenant whose resolved key IS the platform key → pooled.
        graph_mod.build_graph(api_key=platform_key, tenant_id=UUID(int=1))
        # Explicitly-passed platform key with no tenant → platform singleton.
        graph_mod.build_graph(api_key=None, tenant_id=None)
        # Tenant with their OWN distinct key → isolated.
        graph_mod.build_graph(api_key="tenant-private-key", tenant_id=UUID(int=2))

    assert [kw.get("using_platform_key") for kw in recorded] == [True, True, False]
    assert recorded[0]["tenant_id"] == UUID(int=1)
    assert recorded[2]["tenant_id"] == UUID(int=2)
