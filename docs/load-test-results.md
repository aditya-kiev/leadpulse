# Load Test Results

> **Date:** 2026-07-26
> **Environment:** Local Windows (Python 3.14, no Redis, no Gemini API key)
> **Script:** `scripts/load_test.py`
> **Command attempted:**
> ```
> python scripts/load_test.py --base-url http://localhost:8000 --concurrency 30 --requests 10 --concurrency-per-worker 3
> ```

## Result: Could not execute — prerequisites not met in this environment

The load test script is correct and ready to use, but requires two things that were unavailable:

### 1. Gemini API key (`GEMINI_API_KEY` / `gemini_api_key`)

`ChatGoogleGenerativeAI` raises a `ValidationError` at app startup if no API key is set.
The app has no `mock` or `stub` LLM mode — the Gemini client is required at
graph-build time. Every `/webhook/message` request returns HTTP 500 until one is configured.

**Fix needed before running load test:** Set `GEMINI_API_KEY` (env var) or add it to `.env`.

### 2. Redis (`REDIS_URL`)

The app degrades gracefully without Redis (no rate limiting, no caching), but Phase 3
functionality (shared sliding-window rate limiter, CRM push queue, session cache) is
inactive without it. A load test without Redis tests the no-Redis fallback path, not the
production configuration.

**Fix needed for honest capacity numbers:** Start a Redis instance (local `redis-server`,
Docker, or a free-tier Redis Cloud) and set `REDIS_URL` in `.env`.

---

## What will happen when run correctly

Based on the script design and the app's architecture:

### Expected parameters for a 15-50 tenant simulation

| Parameter | Value | Rationale |
|---|---|---|
| `--concurrency` | 30 | ~15-30 tenants × 1-2 simultaneous conversations |
| `--requests` | 10 | 10 messages per simulated conversation |
| `--concurrency-per-worker` | 3 | Max 3 concurrent sends per "tenant" |
| **Total requests** | **300** | 30 × 10 |

### Expected bottlenecks to watch for

1. **Gemini RPM ceiling** — the sliding-window rate limiter (`gemini_rpm_limit`, default
   `10`) will serialize requests after the first 10 across all tenants in any 60-second
   window. At 30 concurrent workers, most requests will queue behind the rate limiter.
   **This is the primary bottleneck for production** — each tenant's 10 requests will
   take roughly 60 seconds to complete at the default RPM limit.

2. **DB connection pool** — default SQLAlchemy pool size (5) may be exhausted at 30
   concurrent workers. Monitor for `TimeoutError` waiting for a connection from the pool.

3. **No Redis fallback** — without Redis, the rate limiter uses a per-process in-memory
   counter (`gemini_call_counter`), which is not shared across workers. Under
   `uvicorn` with multiple workers, each worker gets its own 10-RPM budget, defeating
   the purpose of rate limiting.

### When to re-run

- [ ] After a `GEMINI_API_KEY` is configured in the deployment environment
- [ ] With Redis running and `REDIS_URL` set
- [ ] Against the staging deployment (not local) for realistic network latency
- [ ] After scaling up the app to multiple uvicorn workers

---

## Health check (standalone)

The `/health` endpoint works independently and returns `200 OK` with
`{"status": "ok|degraded", "database": bool, "redis": bool}`. This is the
correct endpoint for uptime monitoring.

```
GET /health → 200
{"status": "ok", "version": "1.0.0", "database": true, "redis": false}
```

---

## Verdict

**Load test script ready, but not yet executed.** The environment (local Windows
without Redis or Gemini API key) cannot produce meaningful capacity numbers.
Running it in staging with both Redis and a valid Gemini API key configured is
a prerequisite before the numbers go into a sales conversation.
