# Phase 1 — Multi-Tenancy Foundation: Migration & Rollback

## Schema Changes

| Change | Type | Details |
|--------|------|---------|
| `organizations` table | New | `id`, `name`, `slug`, `plan_tier`, `is_active`, `brand_name`, `logo_url`, `primary_color`, `custom_domain`, `created_at`, `updated_at` |
| `users` table | New | `id`, `organization_id` (FK → organizations), `email`, `password_hash`, `display_name`, `role`, `is_active`, `created_at`, `updated_at` |
| `crm_configs` table | New | `id`, `organization_id` (FK → organizations), `integration_type`, `config` (JSON), `is_active`, `created_at`, `updated_at` |
| `lead_conversations.tenant_id` | New column | Nullable UUID (FK → organizations), indexed |

All new columns are **nullable** or have **defaults** — zero-downtime additive change.

## Migration Steps

### 1. Apply schema changes

```bash
alembic upgrade 0001
```

This creates the `organizations`, `users`, and `crm_configs` tables, and adds `tenant_id` to `lead_conversations`.

### 2. Backfill existing data

```bash
python -m scripts.backfill_tenants
```

This creates:
- A "Default Organization" for all existing single-tenant data
- An org_admin user (`admin@default.local`) with a generated password (printed to stdout)
- Updates every `lead_conversations` row to set `tenant_id` to the new org's ID

### 3. Verify backfill

Run the script — it logs row counts and confirms all conversations are scoped.

### 4. Enable auth (optional, per-deployment)

Set `AUTH_ENABLED=true` and `JWT_SECRET_KEY=<secure-random-key>` in `.env`.
When ready, switch from the shared API key to JWT-based auth.

During the transition period, the webhook endpoints still accept the master
`X-API-Key` header for backward compatibility.

## Rollback

### Full rollback (undo everything)

```sql
-- 1. Remove tenant_id from conversations
UPDATE lead_conversations SET tenant_id = NULL;

-- 2. Drop new tables
DROP TABLE IF EXISTS crm_configs;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS organizations;

-- 3. Remove the column
ALTER TABLE lead_conversations DROP COLUMN IF EXISTS tenant_id;
```

Or via Alembic:

```bash
alembic downgrade -1
```

### Rollback with data preservation (keep org/users but disable auth)

```bash
# Just disable the feature flag
AUTH_ENABLED=false
```

The `organizations` and `users` tables remain but are unused. Tenant-scoped
queries fall through to the legacy single-tenant behavior.

## What to Watch For

- **Duplicate accounts**: The backfill script is idempotent. Safe to re-run.
- **Admin credentials**: The generated password is printed once to stdout.
  Reset it immediately after first login via the API or directly in the DB.
- **Empty JWT_SECRET_KEY**: If `AUTH_ENABLED=true` but `JWT_SECRET_KEY` is
  empty, JWT operations will fail. Always set this before enabling auth.
