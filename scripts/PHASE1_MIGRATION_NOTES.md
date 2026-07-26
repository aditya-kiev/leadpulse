# Phase 1 — Multi-Tenancy Migration Notes

## What Changed in the DB Schema

### New Tables

| Table | Purpose |
|-------|---------|
| `organizations` | Tenant entities — one per client/brokerage/MGA. |
| `users` | Users with roles (super_admin, org_admin, agent), FK to organizations. |
| `crm_configs` | Per-tenant CRM integration credentials (encrypted), FK to organizations. |

### Modified Tables

| Table | Change |
|-------|--------|
| `lead_conversations` | Added nullable `tenant_id` (FK to organizations). Nullable for backward compat during migration. |

## Migration Order (for production)

1. **Run the schema migration** via Alembic or by running `init_db()` on the updated models:
   ```bash
   alembic upgrade head
   ```
   This adds the three new tables and the nullable `tenant_id` column to `lead_conversations`. Existing single-tenant data is NOT affected — the column is nullable.

2. **Run the backfill script** (`scripts/backfill_tenants.py`):
   ```bash
   python scripts/backfill_tenants.py
   ```
   This creates a "Default Organization" row and assigns all existing `lead_conversations` to it. Also creates an org_admin user with auto-generated credentials (printed to stdout).

   **Idempotent** — safe to run multiple times. Checks if the default org already exists before creating.

3. **Validate** the backfill with the provided row-count queries:
   ```sql
   SELECT count(*) FROM lead_conversations WHERE tenant_id IS NULL;
   -- Should be 0 after backfill
   SELECT o.name, count(lc.id) FROM organizations o
   LEFT JOIN lead_conversations lc ON lc.tenant_id = o.id
   GROUP BY o.name;
   ```

4. **Test** existing clients still work with auth_enabled=False (default). Run the full test suite:
   ```bash
   python -m pytest
   ```

5. **Enable auth** per-deployment by setting `AUTH_ENABLED=true` and `JWT_SECRET_KEY=<secret>` in env. Keep `AUTH_ENABLED=false` on staging until validation is complete.

## Rollback Plan

If something breaks in production:

### Option A: Quick Rollback (no data loss)

1. Set `AUTH_ENABLED=false` (the default). The system reverts to single-tenant mode.
2. Existing API key auth and demo tokens work exactly as before.

### Option B: Full Schema Rollback

If you need to revert the schema:

1. Run the Alembic downgrade:
   ```bash
   alembic downgrade -1
   ```
   This drops `crm_configs`, `users`, `organizations` tables and removes the `tenant_id` column from `lead_conversations`.

2. **Warning**: This destroys user accounts and CRM configs. Any data in those tables is lost. The `tenant_id` values on `lead_conversations` are also removed, but conversation data is preserved.

### Option C: Staged Rollback (preferred)

If auth is causing issues but you want to keep the data model:

1. Keep the schema changes.
2. Keep `AUTH_ENABLED=false`.
3. Fix the auth issue, re-enable when ready.

## Testing Isolation

The cross-tenant isolation test suite (`app/tests/test_tenant_isolation.py`) verifies:

- `get_conversation` filters by `tenant_id` in the WHERE clause
- Conversations created for one tenant are not visible to another
- JWT tokens embed `org_id` for tenant scoping
- super_admin tokens have no `org_id` (cross-tenant access)
- Legacy API key auth works with `auth_enabled=False`
- Local dev works with no auth configured

## Dependencies Added

| Package | Version | Purpose |
|---------|---------|---------|
| `python-jose[cryptography]` | >=3.3.0 | JWT encoding/decoding |
| `passlib[bcrypt]` | >=1.7.4 | Password hashing (with bcrypt backend) |
| `python-multipart` | >=0.0.12 | Form parsing for auth endpoints |

## Env Vars Added

| Var | Default | Purpose |
|-----|---------|---------|
| `AUTH_ENABLED` | `false` | Feature flag — enables JWT auth + tenant scoping |
| `JWT_SECRET_KEY` | `""` | HMAC key for signing JWT tokens |

## Feature Flag Behavior

| `AUTH_ENABLED` | Behavior |
|----------------|----------|
| `false` (default) | Single-tenant mode. `authenticate_request` returns synthetic super_admin identity. API key / demo token auth works as before. |
| `true` | Multi-tenant mode. JWT required for dashboard users. API key / demo token still work for server-to-server integrations with deprecation warning. Every request is tenant-scoped. |
