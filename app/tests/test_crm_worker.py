"""REAL Redis tests for the CRM push retry worker.

The worker (``python -m app.services.crm_worker``) was never started in the
deployment stack (no container, no supervisor) — failed synchronous pushes
were enqueued onto ``crm:push:queue`` by ``enqueue_crm_push`` and stayed
there forever.  These tests exercise the real enqueue → dequeue → process
path against a live Redis so a broken worker loop can't hide behind mocks.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.integrations.base import PushResult
from app.services.redis import close_redis, dequeue_crm_push, enqueue_crm_push, get_redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

WORKER = "app.services.crm_worker"


async def _redis_connected() -> bool:
    await close_redis()
    with patch("app.config.settings.settings.redis_url", REDIS_URL):
        r = await get_redis()
    if r is None:
        return False
    try:
        return await r.ping()
    except Exception:
        return False


@pytest.mark.asyncio
async def test_worker_drains_queued_push():
    """A push enqueued via enqueue_crm_push must be pulled off the queue and
    processed: after process_push runs, the queue is empty again and
    log_crm_push recorded the retry attempt."""
    if not await _redis_connected():
        pytest.skip("Redis not reachable — CRM worker queue tests need a live instance")

    tenant_id = uuid4()
    session_id = f"worker-it-{uuid4()}"
    lead_data = {"lead_name": "Test Lead"}

    # Only the integration resolution is mocked — everything else (enqueue,
    # dequeue, process_push, retry_with_backoff) is the real worker code.
    fake_integration = MagicMock()
    fake_integration.push_lead = AsyncMock(
        return_value=PushResult(success=True, status="created", external_id="ext-1")
    )

    logged: list[dict] = []

    async def fake_log_crm_push(**kwargs):
        logged.append(kwargs)
        return MagicMock()

    try:
        with patch("app.config.settings.settings.redis_url", REDIS_URL), \
             patch(f"{WORKER}.resolve_integration", new_callable=AsyncMock, return_value=fake_integration) as mock_resolve, \
             patch(f"{WORKER}.log_crm_push", new=fake_log_crm_push):
            await close_redis()
            await enqueue_crm_push(tenant_id, session_id, lead_data, "fub")

            r = await get_redis()
            assert r is not None
            assert await r.llen("crm:push:queue") == 1, (
                "payload must be sitting in crm:push:queue after enqueue_crm_push"
            )

            from app.services import crm_worker as worker_mod

            payload = await dequeue_crm_push(timeout=1)
            assert payload is not None, "dequeue_crm_push must pull the queued payload"
            assert payload["session_id"] == session_id
            assert payload["attempt"] == 1

            await worker_mod.process_push(payload)

            # Queue drained — the whole point of the worker.
            assert await r.llen("crm:push:queue") == 0, (
                "queue must be empty after the worker processes the push"
            )

            mock_resolve.assert_awaited_once_with(tenant_id)
            assert len(logged) == 1, "log_crm_push must have recorded the attempt"
            assert logged[0]["status"] == "created"
            assert logged[0]["attempt"] == 2, (
                "retry attempt must be recorded (1 initial attempt + 1 retry)"
            )
            assert logged[0]["session_id"] == session_id
            assert logged[0]["lead_data"] == lead_data
    finally:
        await close_redis()