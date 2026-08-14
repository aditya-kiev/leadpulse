"""In-process scheduler for the daily analytics rollup.

``run_daily_rollup`` was defined but never called anywhere — the
``daily_org_summaries`` table stayed permanently empty and the analytics
dashboard had no pre-computed trend data.  This module runs it once at
startup (backfill for the current window) and then every day at 00:00 UTC.

Upsert semantics in ``crud.upsert_daily_summary`` make repeated runs safe.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_rollup_task: asyncio.Task | None = None


def seconds_until_next_utc_midnight(now: datetime | None = None) -> float:
    """Seconds from ``now`` until the next 00:00 UTC."""
    now = now or datetime.now(timezone.utc)
    next_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(0.0, (next_midnight - now).total_seconds())


async def _rollup_loop() -> None:
    from app.services.analytics import run_daily_rollup

    while True:
        try:
            await run_daily_rollup()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("daily rollup run failed")
        await asyncio.sleep(seconds_until_next_utc_midnight())


def start_daily_rollup_scheduler() -> asyncio.Task:
    """Start (or reuse) the background rollup task. Returns the task."""
    global _rollup_task
    if _rollup_task is not None and not _rollup_task.done():
        return _rollup_task
    _rollup_task = asyncio.create_task(_rollup_loop())
    logger.info("Daily rollup scheduler started (runs now, then each day at 00:00 UTC)")
    return _rollup_task


async def stop_daily_rollup_scheduler() -> None:
    """Cancel the background rollup task if running."""
    global _rollup_task
    if _rollup_task is not None:
        _rollup_task.cancel()
        try:
            await _rollup_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("error while stopping rollup scheduler")
        _rollup_task = None
        logger.info("Daily rollup scheduler stopped")
