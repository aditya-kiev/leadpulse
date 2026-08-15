# Lead Qualification Agent

An AI-powered lead qualification system built with LangGraph, FastAPI, and Google Gemini. It engages website leads via a webhook API, collects qualification data, scores leads against an ICP (real estate / insurance), handles objections, books meetings, and optionally sends SMS notifications.

## Quick Start

```bash
# Dependencies
pip install -r requirements.txt

# Environment (see .env.example or env vars below)
export GEMINI_API_KEY="..."
export DATABASE_URL="postgresql+asyncpg://..."

# Run
uvicorn app.main:app --reload
```

## Deployment (docker compose)

The stack runs three services:

- `api` — the FastAPI application
- `db` — Postgres
- `redis` — Redis (session cache, rate limiting, CRM push retry queue)
- `crm_worker` — standalone background process that drains the
  `crm:push:queue` list and retries failed synchronous CRM pushes

**The `crm_worker` service must be running for CRM push retries to be
processed.** It is started by `docker compose up -d` alongside the `api`;
if it is ever stopped, failed pushes will queue indefinitely in Redis and
never be retried.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | `""` | Google Gemini API key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model name |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent` | Postgres connection |
| `REDIS_URL` | `None` | Optional Redis connection |
| `API_KEY` | `""` | Shared secret for endpoint auth (empty = skip) |
| `ALLOWED_ORIGINS` | `[]` | CORS origins (JSON array) |
| `TWILIO_ACCOUNT_SID` | `""` | Twilio SMS (empty = stub) |
| `TWILIO_AUTH_TOKEN` | `""` | Twilio auth token |
| `TWILIO_FROM_NUMBER` | `""` | Twilio sender number |
| `CALENDLY_API_KEY` | `""` | Calendly API key (empty = stub) |
| `CALENDLY_EVENT_TYPE_URI` | `""` | Calendly event type URI |
| `CALENDLY_USER_URI` | `""` | Calendly user URI |
| `QUALIFICATION_THRESHOLD_HOT` | `0.7` | Score threshold for hot lead |
| `QUALIFICATION_THRESHOLD_WARM` | `0.4` | Score threshold for warm lead |
| `HUMAN_HANDOFF_CONFIDENCE` | `0.3` | Confidence below this → human handoff |
| `VERTICAL` | `generic` | Business persona: `generic`, `real_estate`, or `insurance` |
| `BUSINESS_NAME` | `Your Business Name` | Name used in all lead-facing prompts |
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

### Two-State Persistence

1. **Postgres (authoritative)**: `memory_service.load_state()` / `.save_state()` persists lead fields, `conversation_stage`, and `current_node` across restarts and workers. Merged at the start of every `run_agent()` call.
2. **LangGraph MemorySaver**: Within-`ainvoke` consistency only. Not relied upon across HTTP requests.

## Project Map

| File | Role |
|---|---|
| `app/main.py` | FastAPI app, CORS, router mounting |
| `app/api/webhook.py` | `/webhook/start` and `/webhook/message` endpoints |
| `app/api/conversation.py` | `/conversation/` CRUD endpoints |
| `app/api/debug.py` | `/debug/state` (only when `DEBUG=true`) |
| `app/api/deps.py` | `verify_api_key` dependency |
| `app/agent/graph.py` | LangGraph workflow definition and `run_agent()` |
| `app/agent/state.py` | `AgentState` TypedDict and `get_initial_state()` |
| `app/agent/nodes/` | 8 graph node factories (greeting, info_collection, qualification, faq, objection_handling, meeting_booking, human_handoff, end_conversation) |
| `app/agent/tools/` | CRM stub, calendar (Calendly/stub), lead scoring (ICP), SMS (Twilio/stub), objection detection (LLM) |
| `app/agent/prompts/templates.py` | System prompts for every node |
| `app/config/settings.py` | Pydantic Settings with env var loading |
| `app/database/models.py` | SQLAlchemy `LeadConversation` model |
| `app/services/memory.py` | `MemoryService` for Postgres state persistence |
| `app/tests/` | 40 pytest tests across API, graph, persistence, qualification, SMS, calendar, objection detection |

## API

### `POST /webhook/start`
Start a new conversation.

### `POST /webhook/message`
Continue an existing conversation. Requires `X-API-Key` header when `API_KEY` is set.

### `GET /debug/state/{session_id}`
Inspect raw agent state (only when `DEBUG=true`).

## Tests

```bash
pytest -v
# 40 passed
```
