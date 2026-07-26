"""
Standalone backfill script for Phase 1 multi-tenancy migration.

Migrates an existing single-tenant deployment into the new multi-tenant
data model: creates a default Organization and an org_admin User, then
backfills tenant_id on all existing LeadConversation rows.

Safe to run multiple times (idempotent). Run BEFORE enabling
auth_enabled=True in settings.

Usage:
    python scripts/backfill_tenants.py

Requires DATABASE_URL to be set in environment or .env file.
"""
import asyncio
import logging
import os
import sys
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("backfill_tenants")

DEFAULT_ORG_NAME = "Default Organization"
DEFAULT_ORG_SLUG = "default"
ADMIN_EMAIL = "admin@default-org.local"
ADMIN_PASSWORD = None  # auto-generated


async def backfill():
    database_url = os.getenv("DATABASE_URL") or os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent"
    )
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    engine = create_async_engine(database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        # 1. Check if default org already exists
        result = await session.execute(
            select(text("id")).select_from(text("organizations")).where(
                text("slug = :slug")
            ).params(slug=DEFAULT_ORG_SLUG)
        )
        existing = result.scalar_one_or_none()

        if existing:
            org_id = existing
            logger.info("Default organization already exists: id=%s", org_id)
        else:
            # 2. Create default organization
            org_id = uuid.uuid4()
            now = "NOW()"
            await session.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, plan_tier, is_active, created_at, updated_at) "
                    "VALUES (:id, :name, :slug, :plan_tier, :is_active, :now, :now)"
                ).params(
                    id=org_id,
                    name=DEFAULT_ORG_NAME,
                    slug=DEFAULT_ORG_SLUG,
                    plan_tier="starter",
                    is_active=True,
                    now=text(now),
                )
            )
            logger.info("Created default organization: id=%s", org_id)

        # 3. Backfill tenant_id on existing lead_conversations
        result = await session.execute(
            text("UPDATE lead_conversations SET tenant_id = :org_id WHERE tenant_id IS NULL").params(
                org_id=org_id
            )
        )
        updated_rows = result.rowcount
        logger.info("Backfilled tenant_id on %s lead_conversation(s)", updated_rows)

        # 4. Count conversations now assigned
        result = await session.execute(
            text("SELECT count(*) FROM lead_conversations WHERE tenant_id = :org_id").params(
                org_id=org_id
            )
        )
        total = result.scalar()
        logger.info("Total conversations assigned to default org: %s", total)

        # 5. Create an org_admin user if none exists for this org
        result = await session.execute(
            text("SELECT id FROM users WHERE email = :email").params(email=ADMIN_EMAIL)
        )
        if result.scalar_one_or_none():
            logger.info("Admin user already exists: %s", ADMIN_EMAIL)
        else:
            from app.services.auth import hash_password

            password = ADMIN_PASSWORD or uuid.uuid4().hex[:16]
            user_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO users (id, organization_id, email, password_hash, display_name, role, is_active, created_at, updated_at) "
                    "VALUES (:id, :org_id, :email, :pwd_hash, :name, :role, :active, :now, :now)"
                ).params(
                    id=user_id,
                    org_id=org_id,
                    email=ADMIN_EMAIL,
                    pwd_hash=hash_password(password),
                    name="Default Admin",
                    role="org_admin",
                    active=True,
                    now=text("NOW()"),
                )
            )
            logger.info("=" * 60)
            logger.info("CREATED ORG ADMIN CREDENTIALS")
            logger.info("  Email:    %s", ADMIN_EMAIL)
            logger.info("  Password: %s", password)
            logger.info("=" * 60)

        await session.commit()

    await engine.dispose()
    logger.info("Backfill complete.")


if __name__ == "__main__":
    asyncio.run(backfill())
