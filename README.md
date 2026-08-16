# Lead Qualification Agent

A multi-tenant, AI-powered lead qualification system built with LangGraph, FastAPI, and Google Gemini. One dedicated instance (organization) per client. It engages website leads via a JSON widget (or webhook API), collects qualification data, scores leads against an ICP (real estate / insurance / generic), handles objections, books meetings, sends hot-lead notifications, and pushes qualified leads into the client's CRM.

## Overview

- **Multi-tenant by design**: an `organizations` table is the tenant root. Every tenant has its own `widget_key` (used by the browser widget for auth), its own Gemini API key, vertical / business-name config, branding, CRM config, analytics, and billing status.
- **Onboarding is white-glove**: clients are provisioned via `scripts/onboard_client.py`, not self-serve. A public `/auth/register` endpoint exists (creates an org + `org_admin` user), but the CLI is the primary provisioning path and issues the tenant widget key.
- **Billing is manual-first**: `scripts/manage_billing.py` tracks `trialing | active | past_due | suspended | canceled` and a dunning worklist. An optional Stripe webhook (`POST /billing/webhook`, behind `STRIPE_ENABLED`) automates status transitions. A suspended org's widget stops responding without touching the `is_active` flag.
- **CRM integrations**: per-tenant CRM config, with encrypted credentials (Fernet via `app/integrations/encryption.py`). Supported types: `fub`, `kvcore`, `ams360`, plus a `webhook` fallback (see `app/integrations/registry.py`).
- **Reliability**: a background `crm_worker` drains the Redis retry queue for failed synchronous CRM pushes; a daily analytics rollup scheduler runs at 00:00 UTC; startup guards fail fast on misconfiguration.

## Quick Start

```bash
# Dependencies
pip install -r requirements.txt

# Environment — copy the template and fill it in
cp .env.example .env

# Apply database migrations
alembic upgrade head

# Run
uvicorn app.main:app --reload
```

See [.env.example](.env.example) for every configurable variable.

## Deployment (docker compose)

The stack runs four services:

- `api` — the FastAPI application
- `db` — Postgres
- `redis` — Redis (session cache, rate limiting, CRM push retry queue)
- `crm_worker` — standalone background process that drains the `crm:push:queue`
  list and retries failed synchronous CRM pushes

**The `crm_worker` service must be running for CRM push retries to be
processed.** It is started by `docker compose up -d` alongside the `api`; if it
is ever stopped, failed pushes will queue indefinitely in Redis and never be
retried.

```bash
docker compose up -d
```

`AUTH_ENABLED=true` (and `ENVIRONMENT=production`) are the production flags for
multi-tenant auth; see the environment table below.

## Multi-Tenant Model

- **organizations** — tenant root: slug (unique), `plan_tier`, `is_active`,
  branding (`brand_name`, `primary_color`, `logo_url`), `custom_domain`,
  `widget_key` (client widget auth), `notification_phone`, billing
  (`billing_status`, `billing_provider_customer_id`, `last_payment_at`,
  `next_payment_due_at`).
- **users** — dashboard users, optionally linked to an org (`organization_id`),
  with a `role` (`org_admin`, `agent`, ...) and `is_active`.
- **crm_config** — per-tenant integration record: `integration_type` +
  encrypted `config` + `is_active`.
- **password_reset_tokens** — single-use, expiring JWT-backed reset tokens.
- **lead_conversations** — per-tenant lead threads, scoped by `tenant_id`.

Tenant scoping happens automatically: the widget key / JWT stamps the tenant on
the request, and cross-tenant access is rejected at the query layer.

### Onboarding

The primary path is the CLI:

```bash
python scripts/onboard_client.py \
  --agency "Acme Realty" \
  --slug acme-realty \
  --gemini-key "AIz..." \
  --brand-name "Acme Realty" \
  --primary-color "#9B6B43" \
  --admin-email "owner@acme.com" \
  --notification-phone "+15550123" \
  --widget-out widget-snippet.html \
  --api-base https://api.example.com \
  --app-hostname app.leadpulse.ai
```

This creates the `Organization`, an org_admin `User`, the tenant `widget_key`,
per-tenant Gemini key + branding config, and can emit a ready-to-paste widget
snippet. `--plan-tier` defaults to `starter`.

### Billing

```bash
python scripts/manage_billing.py --list-overdue                 # dunning worklist
python scripts/manage_billing.py acme-realty --mark-paid --customer-id cus_xxx
python scripts/manage_billing.py acme-realty --mark-past-due
python scripts/manage_billing.py acme-realty --suspend          # manual kill switch
python scripts/manage_billing.py acme-realty --reactivate
```

Billing works standalone via manual invoicing (no payment processor required).
To automate it, set `STRIPE_ENABLED=true`, `STRIPE_API_KEY`, and
`STRIPE_WEBHOOK_SECRET`; the `POST /billing/webhook` endpoint then maps
`checkout.session.completed → active`, `invoice.payment_failed → past_due`, and
`customer.subscription.deleted → suspended` (signature-verified). When
`STRIPE_ENABLED` is false the webhook route is not mounted at all.

A `suspended` org keeps `is_active=True` (it still exists) but
`get_organization_by_widget_key` refuses to serve it, so its widget goes quiet
— these two concepts are deliberately separate (`is_active` = exists,
`billing_status` = paid up).

### CRM Integrations

Per tenant, set `integration_type` to one of the supported types and store
credentials (encrypted at rest):

- `fub` — Follow Up Boss (API key)
- `kvcore` — kvCORE (OAuth2)
- `ams360` — AMS360 (token-based)
- `webhook` — generic webhook fallback (also used when no CRM config exists)

Resolved per tenant so each client can point at its own CRM. Failed synchronous
pushes are enqueued for the `crm_worker` retry.

### Auth & Password Reset

- `POST /auth/register` — create org + org_admin (when `organization_name` given).
- `POST /auth/login` — rate limited per `(email, IP)` pair.
- `POST /auth/refresh` — issue a new access token.
- `POST /auth/forgot-password` — always returns 200 (no account enumeration);
  emails a single-use, expiring reset link when the user exists. Rate limited
  per target email.
- `POST /auth/reset-password` — consume the token, set a new password.
- `GET /auth/me` — current user profile.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | `""` | Google Gemini API key (platform default; tenants may override) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model name |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent` | Postgres connection |
| `REDIS_URL` | `None` | Optional Redis connection (session cache, rate limiting, push queue) |
| `API_KEY` | `""` | Master key for webhook/server-to-server auth (required at startup) |
| `AUTH_ENABLED` | `false` | Enable multi-tenant JWT/widget auth |
| `JWT_SECRET_KEY` | `""` | JWT signing key (required when `AUTH_ENABLED=true`) |
| `JWT_ALGORITHM` | `HS256` | JWT algorithm |
| `JWT_ACCESS_TOKEN_TTL_MINUTES` | `30` | Access token lifetime |
| `JWT_REFRESH_TOKEN_TTL_DAYS` | `30` | Refresh token lifetime |
| `PASSWORD_RESET_TOKEN_TTL_MINUTES` | `30` | Reset token lifetime |
| `DEMO_TOKEN_SECRET` | `""` | Demo token signing secret (required at startup) |
| `ALLOWED_ORIGINS` | `[]` | CORS origins (JSON array) |
| `TWILIO_ACCOUNT_SID` | `""` | Twilio SMS (empty = stub) |
| `TWILIO_AUTH_TOKEN` | `""` | Twilio auth token |
| `TWILIO_FROM_NUMBER` | `""` | Twilio sender number |
| `SMS_ENABLED` | `false` | Enable SMS notifications |
| `RESEND_API_KEY` | `""` | Resend email (notifications, password reset) |
| `RESEND_FROM_EMAIL` | `""` | Verified sender address |
| `CALENDLY_API_KEY` | `""` | Calendly API key (empty = stub) |
| `CALENDLY_EVENT_TYPE_URI` | `""` | Calendly event type URI |
| `CALENDLY_USER_URI` | `""` | Calendly user URI |
| `CALENDLY_ENABLED` | `false` | Enable Calendly booking |
| `QUALIFICATION_THRESHOLD_HOT` | `0.7` | Score threshold for hot lead |
| `QUALIFICATION_THRESHOLD_WARM` | `0.4` | Score threshold for warm lead |
| `HUMAN_HANDOFF_CONFIDENCE` | `0.3` | Confidence below this → human handoff |
| `VERTICAL` | `generic` | Business persona: `generic`, `real_estate`, or `insurance` |
| `BUSINESS_NAME` | `Your Business Name` | Name used in all lead-facing prompts |
| `STRIPE_ENABLED` | `false` | Mount the Stripe billing webhook (requires Stripe keys) |
| `STRIPE_API_KEY` | `""` | Stripe API key |
| `STRIPE_WEBHOOK_SECRET` | `""` | Stripe webhook signing secret |
| `CRM_ENCRYPTION_KEY` | `""` | Fernet key for encrypting CRM credentials at rest |
| `FUB_API_KEY` | `""` | Follow Up Boss API key |
| `KVCORE_API_KEY` | `""` | kvCORE API key |
| `KVCORE_API_SECRET` | `""` | kvCORE secret |
| `AMS360_API_KEY` | `""` | AMS360 API key |
| `AMS360_API_SECRET` | `""` | AMS360 secret |
| `SENTRY_DSN` | `""` | Sentry DSN (production) |
| `DEBUG` | `false` | Enable debug router |

## Graph Structure (LangGraph)

```
greeting ──→ info_collection ──→ qualification ──→ handle_next ──→ ...
  │               │                   │                │
  ├→ faq          └→ (loop)           └→ (always        ├→ objection_handling
  └→ info_collection                   handle_next)      ├→ meeting_booking
                                                          ├→ human_handoff
                                                          ├→ faq
                                                          ├→ info_collection
                                                          └→ end
```

- **greeting**: Detect intent (purchase / support / information)
- **info_collection**: Collect missing fields one at a time
- **qualification**: Score lead (budget, timeline, industry, problem, intent) + ICP bonus
- **handle_next**: LLM-based objection detection on last user message
- **objection_handling**: Address pricing / timing / trust / competition / need / authority objections
- **meeting_booking**: Suggest Calendly slots or stub slots, confirm booking
- **human_handoff**: Escalate low-confidence or explicit human requests
- **end**: Summarize and close

### Tenant context in the agent

Each tenant's Gemini key, vertical, and business name are resolved and threaded
into the graph state per session, so every org's widget behaves like its own
instance even on a shared deployment.

### Two-State Persistence

1. **Postgres (authoritative)**: `memory_service.load_state()` / `.save_state()` persists lead fields, `conversation_stage`, and `current_node` across restarts and workers. Merged at the start of every `run_agent()` call.
2. **LangGraph MemorySaver**: Within-`ainvoke` consistency only. Not relied upon across HTTP requests.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook/start` | Start a new conversation |
| `POST` | `/webhook/message` | Continue a conversation (needs `X-Api-Key` / widget key / JWT) |
| `GET` | `/conversation/{session_id}` | Conversation history for a tenant |
| `GET` | `/conversation/` | List conversations for the tenant |
| `POST` | `/demo/token` | Issue a demo token (needs `DEMO_TOKEN_SECRET`) |
| `POST` | `/auth/register` | Create an org + org_admin user |
| `POST` | `/auth/login` | Authenticate → access + refresh tokens (rate limited) |
| `POST` | `/auth/refresh` | Exchange refresh token for new access token |
| `POST` | `/auth/forgot-password` | Email a password-reset link (rate limited per email) |
| `POST` | `/auth/reset-password` | Consume a reset token and set a new password |
| `GET` | `/auth/me` | Current user profile |
| `GET` | `/org/branding` · `PUT /org/branding` | Read / update tenant branding |
| `POST` | `/org/branding/verify-domain` | Verify a custom domain |
| `GET` | `/analytics/metrics` · `/analytics/daily` | Org analytics |
| `GET` | `/analytics/dashboard` | HTML analytics dashboard |
| `POST` | `/billing/webhook` | Stripe webhook (only mounted when `STRIPE_ENABLED`) |
| `GET` | `/health` | DB + Redis health check |
| `GET` | `/debug/state/{session_id}` | Inspect raw agent state (only when `DEBUG=true`) |

Static demo widgets are served at `/demo-realestate` and `/demo-insurance`.

## Project Map

| File | Role |
|---|---|
| `app/main.py` | FastAPI app, CORS, startup guards, router + static mounting |
| `app/api/` | Routers: webhook, conversation, auth, demo, analytics, branding, billing, debug |
| `app/api/deps.py` | `authenticate_request`, `get_current_user`, role guard, rate-limit dependencies |
| `app/agent/` | LangGraph workflow (graph/state/nodes/tools/prompts) + `run_agent()` |
| `app/integrations/` | CRM integrations (`fub`, `kvcore`, `ams360`, `webhook`) + registry + encryption |
| `app/services/` | memory, notifications (Resend), redis (rate limiter, cache, CRM push queue), branding, domain, crm_worker, rollup_scheduler |
| `app/database/` | SQLAlchemy models, session, CRUD helpers |
| `app/config/settings.py` | Pydantic Settings with env var loading |
| `scripts/onboard_client.py` | White-glove client provisioning CLI |
| `scripts/manage_billing.py` | Billing status / dunning CLI |
| `alembic/` | Database migrations |
| `static/` | Marketing + demo HTML pages |
| `app/tests/` | Test suite (see below) |

## Tests

```bash
# Full suite against real Postgres + Redis
TEST_DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test" \
  python -m pytest -q
# 324 passed, 1 failed (the single failure is a known, environment-dependent
# crm_worker race: `test_worker_drains_queued_push` — a live worker drains the
# queue between enqueue and read)
```

The suite runs against a real Postgres + Redis test setup (not mocked DB/Redis
calls). New DB/Redis-touching features are expected to follow that real-infra
pattern (see `app/tests/test_password_reset.py`,
`app/tests/test_auth_rate_limit.py`, `app/tests/test_webhook_rate_limit.py`).
