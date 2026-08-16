"""FIX 5: webhook rate limiter must be scoped per-tenant (not raw-IP-only).

rate_limit_webhook used to key the Redis limiter on the raw client IP, so in
multi-tenant deployments every tenant behind the same egress IP shared one
budget — one tenant's burst throttled everyone, and an attacker hammering one
tenant could deny service to all others.  Now the key includes the tenant_id
when one is stamped on request.state (widget/JWT auth), falling back to the
raw IP only for anonymous traffic.
"""

from types import SimpleNamespace
from unittest.mock import patch

import fakeredis.aioredis
import pytest
from fastapi import HTTPException
from uuid import uuid4

import app.services.redis as redis_mod
from app.api.deps import rate_limit_webhook, reset_webhook_rate_limits


@pytest.fixture(autouse=True)
def _clean_redis_state():
    redis_mod._redis = None
    reset_webhook_rate_limits()
    yield
    redis_mod._redis = None
    reset_webhook_rate_limits()


@pytest.fixture
async def fake_redis():
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis_mod._redis = fake
    with patch("app.services.redis.settings.redis_url", "redis://fake:6379"):
        yield fake
    redis_mod._redis = None


def _req(tenant_id, ip="203.0.113.5"):
    return SimpleNamespace(
        client=SimpleNamespace(host=ip),
        state=SimpleNamespace(tenant_id=tenant_id),
    )


@pytest.mark.asyncio
async def test_two_tenants_same_ip_have_independent_budgets(fake_redis):
    """Tenant A exhausting its budget must NOT throttle Tenant B on the same IP."""
    with patch("app.config.settings.settings.webhook_rpm_limit", 3):
        tenant_a = uuid4()
        tenant_b = uuid4()

        for _ in range(3):
            await rate_limit_webhook(_req(tenant_a))  # A uses all 3 slots

        # A is now over budget → 429
        with pytest.raises(HTTPException) as exc_info:
            await rate_limit_webhook(_req(tenant_a))
        assert exc_info.value.status_code == 429

        # B has its OWN budget on the same IP → still allowed
        for _ in range(3):
            await rate_limit_webhook(_req(tenant_b))
        with pytest.raises(HTTPException) as exc_info_b:
            await rate_limit_webhook(_req(tenant_b))
        assert exc_info_b.value.status_code == 429


@pytest.mark.asyncio
async def test_tenant_scoped_key_does_not_leak_between_tenants(fake_redis):
    """Redis keys must differ by tenant_id so budgets stay isolated."""
    with patch("app.config.settings.settings.webhook_rpm_limit", 5):
        tenant_a = uuid4()
        tenant_b = uuid4()
        for _ in range(5):
            await rate_limit_webhook(_req(tenant_a))
        await rate_limit_webhook(_req(tenant_b))  # must not trip A's limit
        assert True  # reaching here means B was allowed


@pytest.mark.asyncio
async def test_anonymous_traffic_falls_back_to_ip_key(fake_redis):
    """No tenant_id (anonymous / super_admin API key) → raw-IP budget."""
    with patch("app.config.settings.settings.webhook_rpm_limit", 2):
        for _ in range(2):
            await rate_limit_webhook(_req(tenant_id=None))
        with pytest.raises(HTTPException) as exc_info:
            await rate_limit_webhook(_req(tenant_id=None))
        assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_inmem_fallback_is_per_tenant_scope():
    """Redis down: the in-memory fallback must also be scoped per-tenant."""
    with patch("app.services.redis.get_redis", return_value=None), \
         patch("app.config.settings.settings.webhook_rpm_limit", 2):
        tenant_a = uuid4()
        tenant_b = uuid4()
        for _ in range(2):
            await rate_limit_webhook(_req(tenant_a))
        with pytest.raises(HTTPException):
            await rate_limit_webhook(_req(tenant_a))
        # B untouched on the same IP
        for _ in range(2):
            await rate_limit_webhook(_req(tenant_b))
        with pytest.raises(HTTPException):
            await rate_limit_webhook(_req(tenant_b))
