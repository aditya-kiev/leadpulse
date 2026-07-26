# Phase 3 — Reliability at Scale: Migration & Rollback

## What Changed

### Settings (no DB migration needed for Phase 3)
- **`app/config/settings.py`**: Added `environment` (str, default "development") and `sentry_dsn` (str, default "").

### New files
| File | Purpose |
|------|---------|
| `app/services/redis.py` | Redis client lazy init, sliding-window rate limiter, session cache, tenant config cache, CRM push queue |
| `app/services/logging_config.py` | `JSONFormatter` (structured JSON logs) and `configure_logging()` — auto-switches to JSON in production |
| `app/services/crm_worker.py` | Standalone background worker that polls Redis for queued CRM pushes and retries them |
| `scripts/load_test.py` | Asyncio-based load testing script simulating concurrent tenants |

### Modified files
| File | Change |
|------|--------|
| `app/main.py` | Uses `configure_logging()` instead of raw `basicConfig`; added request-ID middleware; enhanced `/health` to check DB + Redis; Redis/Sentry init in lifespan |
| `app/models/schemas.py` | `HealthOut` now includes `database` and `redis` boolean fields |
| `app/agent/gemini.py` | Rate limiter tries Redis-backed sliding window first, falls back to in-memory `deque` |
| `app/services/memory.py` | `load_state` caches in Redis (TTL 5 min), `save_state` invalidates cache |
| `app/agent/tools/crm.py` | On push failure after retries, enqueues to Redis-backed retry queue |
| `requirements.txt` | Added `sentry-sdk>=2.20.0` |
| `.env.example` | Added `ENVIRONMENT`, `SENTRY_DSN` |

### Redis key namespace
| Key pattern | Purpose | TTL |
|-------------|---------|-----|
| `ratelimit:gemini:*` | Sliding-window timestamps for Gemini RPM | 61s |
| `session:{session_id}` | Cached conversation state | 300s |
| `tenant:{uuid}:config` | Cached CRM/resolved config | 120s |
| `crm:push:queue` | List of failed CRM pushes for background retry | N/A (list) |

## Rollback

This phase is fully additive — no DB schema changes, no destructive migrations.

### Quick rollback (revert to Phase 2 behavior)
1. Revert `app/main.py` to the Phase 2 version (simple logging, static health, no Redis/Sentry init)
2. Revert `app/agent/gemini.py` to the in-memory-only rate limiter
3. Revert `app/services/memory.py` to no caching
4. Revert `app/agent/tools/crm.py` to no enqueue on failure
5. Remove new files: `app/services/redis.py`, `app/services/logging_config.py`, `app/services/crm_worker.py`, `scripts/load_test.py`
6. Revert `requirements.txt` (remove sentry-sdk)
7. Revert `.env.example`

The new features degrade gracefully:
- **No Redis**: Everything falls back to in-memory/DB-only behavior. Rate limiter uses the per-process deque, memory service reads from Postgres, CRM push queue doesn't enqueue.
- **No Sentry DSN**: Sentry is only initialized if `sentry_dsn` is set. Unset DSN = no-op.
- **Structured logging**: Only active when `ENVIRONMENT=production`. Development retains human-readable format.

## Testing
- The existing test suite covers all in-memory fallback paths
- To test with Redis: set `REDIS_URL` in `.env`, start a Redis instance, run tests
- To test Sentry: set `SENTRY_DSN` in `.env`
