import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_with_backoff(
    fn: Callable[..., Awaitable[T]],
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    **kwargs,
) -> T:
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                jitter = random.uniform(0, delay * 0.1)
                total = delay + jitter
                logger.warning(
                    "retry attempt %d/%d failed: %s — retrying in %.2fs",
                    attempt + 1, max_retries, e, total,
                )
                await asyncio.sleep(total)
    raise last_exc
