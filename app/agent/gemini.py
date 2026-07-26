import asyncio
import logging
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.config.settings import settings

logger = logging.getLogger(__name__)

# Per-turn Gemini call counter (reset per run_agent invocation)
gemini_call_counter: ContextVar[int] = ContextVar("gemini_call_counter", default=0)

# Override RPM limit for demo-token-authenticated requests (set per-request)
demo_rpm_limit: ContextVar[int] = ContextVar("demo_rpm_limit", default=0)

# In-memory fallback rate limiter (used when Redis is unavailable)
_sliding_lock: asyncio.Lock | None = None
_sliding_timestamps: deque[float] | None = None
_redis_limiter: Any = None


def _get_fallback_limiter() -> tuple[asyncio.Lock, deque[float]]:
    global _sliding_lock, _sliding_timestamps
    if _sliding_lock is None:
        _sliding_lock = asyncio.Lock()
        _sliding_timestamps = deque()
    return _sliding_lock, _sliding_timestamps


async def _acquire_rate_limit() -> None:
    """Wait until a slot opens in the 60-second sliding window.

    Uses Redis-backed sliding window when Redis is available, falls back
    to the per-process in-memory deque.
    """
    override = demo_rpm_limit.get()
    rpm_limit = int(override if override > 0 else settings.gemini_rpm_limit)
    if rpm_limit <= 0:
        return

    # Try Redis-backed limiter first
    global _redis_limiter
    if _redis_limiter is None and settings.redis_url:
        from app.services.redis import RedisSlidingWindowRateLimiter
        _redis_limiter = RedisSlidingWindowRateLimiter()

    if _redis_limiter is not None:
        wait = await _redis_limiter.acquire(rpm_limit)
        if wait == 0:
            return
        if wait > 0:
            await asyncio.sleep(wait)
            return
        # wait == -1 means Redis unavailable — fall through

    # In-memory fallback
    lock, timestamps = _get_fallback_limiter()
    while True:
        async with lock:
            now = time.monotonic()
            cutoff = now - 60.0
            while timestamps and timestamps[0] < cutoff:
                timestamps.popleft()

            if len(timestamps) < rpm_limit:
                timestamps.append(now)
                return

            wait = timestamps[0] + 60.0 - now

        await asyncio.sleep(wait)


class GeminiRateLimitError(Exception):
    """Raised when Gemini is rate limited after retrying."""


def _iter_exception_chain(error: BaseException):
    current: BaseException | None = error
    seen: set[int] = set()

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_rate_limited_gemini_error(error: BaseException) -> bool:
    for current in _iter_exception_chain(error):
        code = getattr(current, "code", None)
        status_code = getattr(current, "status_code", None)
        status = getattr(current, "status", None)
        message = str(current).upper()

        if code == 429 or status_code == 429:
            return True
        if status == "RESOURCE_EXHAUSTED":
            return True
        if "RESOURCE_EXHAUSTED" in message and "429" in message:
            return True

    return False


def _parse_client_error_retry_info(details: dict) -> float | None:
    """Extract retry delay from ``google.genai.errors.ClientError.details``.

    The dict has the shape ``{"error": {"details": [{"@type": "...RetryInfo",
    "retryDelay": "5s"}, ...]}}`` — no protobuf involved.
    """
    error = details.get("error", {})
    if not isinstance(error, dict):
        return None
    for detail in error.get("details", []):
        if not isinstance(detail, dict):
            continue
        if detail.get("@type", "").endswith("RetryInfo"):
            retry_delay = detail.get("retryDelay", "")
            if not retry_delay:
                continue
            try:
                if retry_delay.endswith("s"):
                    return float(retry_delay[:-1])
                return float(retry_delay)
            except ValueError:
                continue
    return None


def _get_retry_delay(error: BaseException) -> float | None:
    """Try to extract a server-suggested retry delay from the exception.

    Priority:
    1. ``retry_after`` attribute (set by google-api-core for 429 responses)
    2. ``google.genai.ClientError`` dict-style details (``@type`` ending in
       ``RetryInfo`` with a ``retryDelay`` string like ``"5s"``)
    3. ``google.rpc.RetryInfo`` from protobuf ``Any`` (legacy path)
    4. Fall back to *None* (caller uses exponential backoff instead)
    """
    for current in _iter_exception_chain(error):
        retry_after = getattr(current, "retry_after", None)
        if retry_after is not None:
            return float(retry_after)

        # google.genai.errors.ClientError exposes .details as a dict
        details = getattr(current, "details", None)
        if isinstance(details, dict):
            delay = _parse_client_error_retry_info(details)
            if delay is not None:
                return delay

        # Protobuf RetryInfo (legacy, e.g. google-api-core)
        details = getattr(current, "error_details", None) or details
        if details and not isinstance(details, dict):
            try:
                from google.protobuf import any_pb2
                from google.rpc import retry_info_pb2

                for detail in details:
                    if isinstance(detail, any_pb2.Any):
                        ri = retry_info_pb2.RetryInfo()
                        if detail.Unpack(ri):
                            nanos = ri.retry_delay.nanos or 0
                            seconds = ri.retry_delay.seconds or 0
                            if seconds > 0 or nanos > 0:
                                return seconds + nanos / 1_000_000_000
            except ImportError:
                pass

    return None


_last_usage: ContextVar[dict | None] = ContextVar("_last_usage", default=None)


def get_last_usage() -> dict | None:
    return _last_usage.get()


def _extract_usage(response: Any) -> dict:
    usage = {}
    try:
        meta = getattr(response, "response_metadata", {}) or {}
        if "usage_metadata" in meta:
            usage = dict(meta["usage_metadata"])
        elif hasattr(response, "usage_metadata"):
            usage = dict(response.usage_metadata)
    except Exception:
        pass
    return usage


_GEMINI_COST_PER_1K_INPUT = 0.000075
_GEMINI_COST_PER_1K_OUTPUT = 0.00030


def estimate_gemini_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens / 1000) * _GEMINI_COST_PER_1K_INPUT + (completion_tokens / 1000) * _GEMINI_COST_PER_1K_OUTPUT


@dataclass
class RetryingGeminiModel:
    """Thin proxy that retries transient Gemini rate-limit failures."""

    model: Any
    max_retries: int = 2
    initial_backoff_seconds: float = 0.5
    tenant_id: UUID | None = None
    session_id: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.model, name)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        counter = gemini_call_counter
        counter.set(counter.get() + 1)

        delay = self.initial_backoff_seconds
        attempts = self.max_retries + 1

        for attempt in range(1, attempts + 1):
            await _acquire_rate_limit()

            try:
                response = await self.model.ainvoke(*args, **kwargs)
                usage = _extract_usage(response)
                _last_usage.set(usage)
                return response
            except Exception as exc:
                if not _is_rate_limited_gemini_error(exc):
                    raise

                logger.exception(
                    "Gemini rate limit encountered (attempt %s/%s)",
                    attempt,
                    attempts,
                )

                if attempt >= attempts:
                    raise GeminiRateLimitError(
                        "The AI service is temporarily rate limited. "
                        "Please try again in a minute."
                    ) from exc

                suggested = _get_retry_delay(exc)
                sleep_for = suggested if suggested is not None else delay
                await asyncio.sleep(sleep_for)
                delay *= 2
