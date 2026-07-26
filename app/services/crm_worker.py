"""
Background worker for processing the CRM push retry queue.

Runs as a standalone process that polls Redis for queued CRM push attempts
and retries them with a delay between attempts.

Usage:
    python -m app.services.crm_worker

Or in production as a systemd service / supervisor process.
"""

import asyncio
import json
import logging
from uuid import UUID

from app.integrations.registry import resolve_integration
from app.integrations.retry import retry_with_backoff
from app.database.crud import log_crm_push
from app.services.redis import dequeue_crm_push, get_redis, close_redis

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
BACKOFF_DELAY = 5.0


async def process_push(payload: dict) -> None:
    tenant_id = UUID(payload["tenant_id"])
    session_id = payload["session_id"]
    lead_data = payload["lead_data"]
    integration_type = payload["integration_type"]
    attempt = payload.get("attempt", 1)

    logger.info("CRM worker processing push tenant=%s session=%s attempt=%s",
                tenant_id, session_id, attempt)

    try:
        integration = await resolve_integration(tenant_id)
        result = await retry_with_backoff(
            integration.push_lead,
            lead_data,
            max_retries=2,
            base_delay=BACKOFF_DELAY,
        )
        await log_crm_push(
            organization_id=tenant_id,
            integration_type=integration_type,
            session_id=session_id,
            status=result.status if result else "failed",
            attempt=attempt + 1,
            lead_data=lead_data,
            response_data=result.raw_response if result else None,
            error_message=result.error_message if result else None,
        )
        if result.success:
            logger.info("CRM worker push SUCCESS tenant=%s session=%s", tenant_id, session_id)
        else:
            logger.warning("CRM worker push FAILED tenant=%s session=%s attempt=%s",
                           tenant_id, session_id, attempt)
    except Exception as e:
        logger.error("CRM worker push EXCEPTION tenant=%s session=%s: %s",
                     tenant_id, session_id, e)
        await log_crm_push(
            organization_id=tenant_id,
            integration_type=integration_type,
            session_id=session_id,
            status="worker_failed",
            attempt=attempt + 1,
            lead_data=lead_data,
            error_message=str(e),
        )


async def run_worker():
    logger.info("CRM push worker starting...")
    r = await get_redis()
    if r is None:
        logger.warning("Redis unavailable, CRM push worker cannot start")
        return

    logger.info("CRM push worker polling for jobs...")
    while True:
        try:
            payload = await dequeue_crm_push(timeout=5)
            if payload is not None:
                await process_push(payload)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("CRM worker error: %s", e)
            await asyncio.sleep(1)

    await close_redis()
    logger.info("CRM push worker stopped")


def main():
    import time as _time
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger.info("Starting CRM push worker process")
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("CRM push worker interrupted")


if __name__ == "__main__":
    main()
