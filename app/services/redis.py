import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from app.config.settings import settings

logger = logging.getLogger(__name__)

_redis = None


async def get_redis():
    global _redis
    if _redis is None:
        from redis.asyncio import Redis, from_url
        if not settings.redis_url:
            logger.info("REDIS_URL not set — running without Redis")
            _redis = None
        else:
            try:
                _redis = from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                    retry_on_timeout=False,
                )
                await _redis.ping()
                logger.info("Redis connected: %s", settings.redis_url)
            except Exception as e:
                logger.warning("Redis unavailable, running without Redis: %s", e)
                _redis = None
    return _redis


async def close_redis():
    global _redis
    if _redis is not None:
        try:
            await _redis.close()
        except Exception:
            pass
        _redis = None


async def is_redis_available() -> bool:
    r = await get_redis()
    if r is None:
        return False
    try:
        return await r.ping()
    except Exception:
        return False


# ── Rate Limiter (sliding window via sorted sets) ────────────────────────

_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local cutoff = now_ms - window_ms
redis.call("ZREMRANGEBYSCORE", key, 0, cutoff)
local count = redis.call("ZCARD", key)
if count < limit then
    local seq = redis.call("INCR", key .. ":seq")
    redis.call("ZADD", key, now_ms, seq)
    redis.call("EXPIRE", key, math.ceil(window_ms / 1000) + 1)
    redis.call("EXPIRE", key .. ":seq", math.ceil(window_ms / 1000) + 1)
    return 0
end
local oldest_members = redis.call("ZRANGE", key, 0, 0)
local oldest_score = 0
if #oldest_members > 0 then
    oldest_score = redis.call("ZSCORE", key, oldest_members[1])
end
local retry_after_sec = (oldest_score + window_ms - now_ms) / 1000
if retry_after_sec < 0 then retry_after_sec = 0 end
return math.ceil(retry_after_sec * 1000) / 1000
"""


class RedisSlidingWindowRateLimiter:
    def __init__(self, key_prefix: str = "ratelimit:gemini"):
        self._key_prefix = key_prefix

    async def acquire(self, rpm_limit: int, window: int = 60) -> float:
        """Acquire a slot. Returns 0 on success, or seconds to wait if denied."""
        if rpm_limit <= 0:
            return 0
        r = await get_redis()
        if r is None:
            return -1
        # Use a fixed global key so all workers share the same rate-limit budget
        key = f"{self._key_prefix}:global"
        now_ms = int(__import__("time").time() * 1000)
        window_ms = window * 1000
        try:
            result = await r.eval(_RATE_LIMIT_SCRIPT, 1, key, str(now_ms), str(window_ms), str(rpm_limit))
            if isinstance(result, list):
                result = result[0] if result else 0
            return float(result or 0)
        except Exception as e:
            logger.warning("Redis rate limiter error, falling back: %s", e)
            return -1


# ── Session cache ────────────────────────────────────────────────────────

async def cache_session_state(session_id: str, state: dict, ttl: int = 300) -> None:
    r = await get_redis()
    if r is None:
        return
    try:
        key = f"session:{session_id}"
        await r.setex(key, ttl, json.dumps(state, default=str))
    except Exception as e:
        logger.debug("cache_session_state error: %s", e)


async def get_cached_session_state(session_id: str) -> dict | None:
    r = await get_redis()
    if r is None:
        return None
    try:
        key = f"session:{session_id}"
        data = await r.get(key)
        if data:
            return json.loads(data)
        return None
    except Exception as e:
        logger.debug("get_cached_session_state error: %s", e)
        return None


async def invalidate_session_cache(session_id: str) -> None:
    r = await get_redis()
    if r is None:
        return
    try:
        await r.delete(f"session:{session_id}")
    except Exception:
        pass


async def cache_tenant_config(tenant_id: UUID, config: dict, ttl: int = 120) -> None:
    r = await get_redis()
    if r is None:
        return
    try:
        key = f"tenant:{tenant_id}:config"
        await r.setex(key, ttl, json.dumps(config, default=str))
    except Exception as e:
        logger.debug("cache_tenant_config error: %s", e)


async def get_cached_tenant_config(tenant_id: UUID) -> dict | None:
    r = await get_redis()
    if r is None:
        return None
    try:
        key = f"tenant:{tenant_id}:config"
        data = await r.get(key)
        if data:
            return json.loads(data)
        return None
    except Exception as e:
        logger.debug("get_cached_tenant_config error: %s", e)
        return None


# ── CRM push retry queue ─────────────────────────────────────────────────

async def enqueue_crm_push(tenant_id: UUID, session_id: str, lead_data: dict, integration_type: str) -> None:
    r = await get_redis()
    if r is None:
        return
    try:
        payload = json.dumps({
            "tenant_id": str(tenant_id),
            "session_id": session_id,
            "lead_data": lead_data,
            "integration_type": integration_type,
            "attempt": 1,
        }, default=str)
        await r.lpush("crm:push:queue", payload)
        logger.info("CRM push enqueued tenant=%s session=%s", tenant_id, session_id)
    except Exception as e:
        logger.warning("enqueue_crm_push error: %s", e)


async def dequeue_crm_push(timeout: int = 5) -> dict | None:
    r = await get_redis()
    if r is None:
        return None
    try:
        result = await r.brpop("crm:push:queue", timeout=timeout)
        if result:
            _, payload = result
            return json.loads(payload)
        return None
    except Exception as e:
        logger.debug("dequeue_crm_push error: %s", e)
        return None
