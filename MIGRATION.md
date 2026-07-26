# Alembic Migration Guide

## Overview

This project uses **Alembic** for schema migrations. The first version
(`001_initial_schema`) is a **baseline** snapshot of all 7 tables created
across Phases 1–5 (and subsequent fixes). Future schema changes are written
as new Alembic revision files.

## Quick reference

| Command | When to use |
|---------|-------------|
| `alembic upgrade head` | Apply all pending migrations |
| `alembic downgrade -1` | Undo the last migration |
| `alembic current` | Show which revision the DB is at |
| `alembic history` | Show full migration chain |
| `alembic revision --autogenerate -m "desc"` | Generate a new migration from model changes |
| `alembic stamp head` | Mark the DB as at head **without** running SQL (for existing DBs) |

## First-time setup for existing databases

If you already have tables created automatically via
`Base.metadata.create_all` (the pre-Alembic approach), **do not** run
`alembic upgrade head` — it will fail because the tables already exist.

Instead, tell Alembic that your DB is already at the baseline:

```bash
cd lead-agent
alembic stamp head
```

This writes the `001_initial_schema` revision to `alembic_version` without
executing any DDL.

## For new databases

```bash
cd lead-agent
alembic upgrade head
```

This creates all 7 tables from scratch.

## Adding new migrations

After editing models in `app/database/models.py`:

```bash
cd lead-agent
alembic revision --autogenerate -m "description_of_change"
alembic upgrade head
```

Review the generated file in `alembic/versions/` before applying.

## Downgrading (rollback)

```bash
alembic downgrade -1      # undo last migration
alembic downgrade 001_initial_schema  # back to baseline
alembic downgrade base    # drop everything
```

---

# Phase 1 — Multi-Tenancy Migration

## Schema changes

| Change | Type | Affected tables |
|--------|------|-----------------|
| New table `organizations` | Additive | — |
| New table `users` | Additive | — |
| New table `crm_configs` | Additive | — |
| New column `tenant_id` on `lead_conversations` | Additive (nullable FK) | `lead_conversations` |

All changes are **additive** — no existing columns or tables are dropped or
altered in a breaking way. `tenant_id` is `NULLABLE` so existing rows without
a tenant assignment continue to work.

## Migration order

### Step 1: Deploy schema + code with `AUTH_ENABLED=false`

1. Deploy the new code (with the multi-tenancy models and auth system).
2. Set `AUTH_ENABLED=false` in your `.env` (this is the default).
3. The app startup runs `Base.metadata.create_all` which creates the new
   tables (`organizations`, `users`, `crm_configs`) and adds the nullable
   `tenant_id` column to `lead_conversations`.
4. **Existing single-tenant behavior is unchanged** — the old `API_KEY` auth
   still works exactly as before. Existing clients are unaffected.

### Step 2: Run the backfill script

```bash
cd lead-agent
python scripts/backfill_tenants.py
```

This creates:
- A default `Organization` (name: "Default Organization", slug: "default")
- An `org_admin` user account with auto-generated password (printed to stdout)
- Backfills `tenant_id` on all existing `lead_conversations` rows

The script is **idempotent** — safe to run multiple times.

### Step 3: Verify data integrity

Run a spot-check to confirm backfill was correct:

```sql
-- All lead_conversations should have a tenant_id
SELECT count(*) FROM lead_conversations WHERE tenant_id IS NULL;

-- Each tenant_id should reference a valid organization
SELECT lc.tenant_id, o.name
FROM lead_conversations lc
LEFT JOIN organizations o ON lc.tenant_id = o.id
WHERE o.id IS NULL;
```

Row-count verification:
```sql
SELECT count(*) FROM lead_conversations;  -- pre-backfill count
SELECT count(*) FROM lead_conversations WHERE tenant_id IS NOT NULL;  -- should match
```

### Step 4: Enable multi-tenant auth

1. Set `AUTH_ENABLED=true` in your `.env`.
2. Deploy.
3. Verify:
   - Existing API key still works (master key).
   - New JWT auth works (login with the auto-generated admin credentials).
   - All existing conversations are accessible through the correct tenant.

## Rollback plan

If something breaks in production:

### Rollback the code (immediate fix)

1. Revert to the previous deployment (before Phase 1 changes).
2. Set `AUTH_ENABLED=false` if the new code is still running.
3. The new tables (`organizations`, `users`, `crm_configs`) and the
   `tenant_id` column are **ignored** by the old code — no SQL rollback
   is needed for an immediate revert.

### Rollback the schema (if needed)

```sql
BEGIN;

-- 1. Remove the tenant_id column (null values are fine, existing data is preserved)
ALTER TABLE lead_conversations DROP COLUMN IF EXISTS tenant_id;

-- 2. Drop new tables (order matters due to FK constraints)
DROP TABLE IF EXISTS crm_configs CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS organizations CASCADE;

COMMIT;
```

Or using Alembic:
```bash
alembic downgrade 001_initial_schema
```

### Data safety guarantees

- The `tenant_id` column is nullable — dropping it loses no data.
- The backfill script only INSERTs — it never DELETEs or UPDATEs existing
  lead data.
- The new auth system is gated by `AUTH_ENABLED=false` — setting it back to
  `false` restores the old behavior immediately.

## Rollout safety checklist

- [ ] Step 1 deployed and verified in production with `AUTH_ENABLED=false`
- [ ] Backfill script run against a staging copy of production data first
- [ ] Row counts verified after backfill (production)
- [ ] New auth system tested on staging with a copy of production data
- [ ] `AUTH_ENABLED=true` enabled on staging first, then production
- [ ] Existing clients notified of new login credentials (or auto-generated)
- [ ] Rollback plan documented and accessible to the on-call engineer

## Key risks

| Risk | Mitigation |
|------|------------|
| Cross-tenant data leakage | Tenant isolation test suite must pass before enabling `AUTH_ENABLED=true` |
| Existing client auth breaks | `verify_api_key` still works when `auth_enabled=false`; master API key works when `auth_enabled=true` |
| Lost tenant_id during backfill | Script is idempotent; verify with SQL spot-check before enabling tenant auth |
| Demo tokens break | Demo token path is preserved in `authenticate_request` dependency |
