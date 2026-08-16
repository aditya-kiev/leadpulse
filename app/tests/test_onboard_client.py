import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

sys.path.insert(0, ".")

from scripts.onboard_client import (  # noqa: E402
    DEFAULT_WIDGET_TITLE,
    VALID_VERTICALS,
    _setup_checklist,
    _widget_snippet,
    onboard_client,
    parse_args,
    slugify,
)


# ── REAL Postgres regression test ─────────────────────────────────────────
# onboard_client() never assigned admin_email = args.admin_email, so even when
# --admin-email was passed the checklist took the "no admin created" branch,
# the temporary password was never shown, and the operator could not log in.

import os
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test",
)


@pytest.fixture
async def pg_session_factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_onboard_with_admin_email_prints_password_and_persists_user(pg_session_factory):
    """REAL Postgres: onboarding with --admin-email must produce a checklist
    that announces the dashboard admin user AND the temporary password, and
    the password must actually verify against the stored bcrypt hash.

    Before the fix, admin_email stayed None, so the checklist always said
    'create one via /auth/register' and no password was ever printed."""
    from app.database.models import CRMConfig, Organization, User
    from app.services.auth import verify_password

    agency = f"IT Onboard {uuid4().hex[:6]}"
    slug = f"it-onboard-{uuid4().hex[:8]}"
    admin_email = f"owner-{uuid4().hex[:8]}@example.com"
    org_id = None

    try:
        args = SimpleNamespace(
            agency=agency,
            vertical="real_estate",
            gemini_key="test-gemini-key",
            slug=slug,
            brand_name=agency,
            primary_color="#9B6B43",
            logo_url=None,
            plan_tier="starter",
            admin_email=admin_email,
            notification_phone=None,
            widget_out=None,
            api_base="https://api.example.com",
            app_hostname="app.leadpulse.ai",
            widget_title=None,
        )

        enc_key = "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="
        with patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.crm_encryption_key", enc_key):
            result = await onboard_client(args)

        checklist = result.checklist
        assert f"[x] Dashboard admin user       {admin_email}" in checklist, checklist
        assert "-> temporary password:" in checklist, checklist

        password_line = [l for l in checklist.splitlines() if "temporary password" in l]
        assert password_line, "temporary password line missing from checklist"
        printed_password = password_line[0].split(":", 1)[1].strip().strip()
        assert printed_password, "temporary password must be non-empty"

        org_id = result.org["id"]
        async with pg_session_factory() as session:
            from sqlalchemy import select
            user = (await session.execute(
                select(User).where(User.email == admin_email)
            )).scalar_one_or_none()
            assert user is not None, "org_admin user must exist in Postgres"
            assert user.organization_id is not None
            assert user.role == "org_admin"
            assert verify_password(printed_password, user.password_hash), (
                "printed password must verify against stored bcrypt hash"
            )
    finally:
        async with pg_session_factory() as session:
            if org_id:
                await session.execute(delete(User).where(User.organization_id == org_id))
                await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == org_id))
                org = (await session.execute(
                    select(Organization.id).where(Organization.id == org_id)
                )).scalar_one_or_none()
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.asyncio
async def test_onboard_without_admin_email_notes_manual_step(pg_session_factory):
    """Without --admin-email, the checklist must keep the manual-step note (no
    user row created)."""
    from app.database.models import Organization, User
    slug = f"it-onboard-{uuid4().hex[:8]}"
    org_id = None
    try:
        args = SimpleNamespace(
            agency=f"NoAdmin {uuid4().hex[:6]}",
            vertical="generic",
            gemini_key="test-gemini-key",
            slug=slug,
            brand_name=None,
            primary_color=None,
            logo_url=None,
            plan_tier="starter",
            admin_email=None,
            notification_phone=None,
            widget_out=None,
            api_base=None,
            app_hostname=None,
            widget_title=None,
        )
        enc_key = "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="
        with patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.crm_encryption_key", enc_key):
            result = await onboard_client(args)
        assert "--admin-email" in result.checklist
        org_id = result.org["id"]
        async with pg_session_factory() as session:
            from sqlalchemy import select
            users = (await session.execute(
                select(User).where(User.organization_id == org_id)
            )).scalars().all()
            assert users == []
    finally:
        async with pg_session_factory() as session:
            if org_id:
                from app.database.models import CRMConfig
                await session.execute(delete(User).where(User.organization_id == org_id))
                await session.execute(delete(CRMConfig).where(CRMConfig.organization_id == org_id))
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


@pytest.mark.asyncio
async def test_onboard_no_encryption_key_never_silently_base64_encodes(pg_session_factory):
    """REAL Postgres regression: with AUTH_ENABLED=true + ENVIRONMENT=development
    and NO CRM_ENCRYPTION_KEY, onboard_client() must raise (it would otherwise
    store the tenant's Gemini key as reversible plaintext). It must NOT succeed
    and return a checklist."""
    from app.database.models import Organization, User

    slug = f"it-onboard-{uuid4().hex[:8]}"
    org_id = None
    try:
        args = SimpleNamespace(
            agency=f"NoKey {uuid4().hex[:6]}",
            vertical="generic",
            gemini_key="tenant-secret-gemini-key",
            slug=slug,
            brand_name=None,
            primary_color=None,
            logo_url=None,
            plan_tier="starter",
            admin_email="owner@nokey.example.com",
            notification_phone=None,
            widget_out=None,
            api_base=None,
            app_hostname=None,
            widget_title=None,
        )
        with patch("app.database.session.async_session_factory", pg_session_factory), \
             patch("app.config.settings.settings.environment", "development"), \
             patch("app.config.settings.settings.auth_enabled", True), \
             patch("app.config.settings.settings.crm_encryption_key", ""):
            with pytest.raises(RuntimeError):
                await onboard_client(args)

        async with pg_session_factory() as session:
            from sqlalchemy import select
            org = (await session.execute(
                select(Organization.id).where(Organization.slug == slug)
            )).scalar_one_or_none()
            assert org is None, "no org must be committed when encryption is unavailable"
    finally:
        async with pg_session_factory() as session:
            if org_id:
                await session.execute(delete(User).where(User.organization_id == org_id))
                await session.execute(delete(Organization).where(Organization.id == org_id))
                await session.commit()


class TestSlugify:
    def test_basic(self):
        assert slugify("Bella Vista Realty") == "bella-vista-realty"

    def test_strips_special_chars(self):
        assert slugify("   Acme!! Group  ") == "acme-group"

    def test_empty_falls_back(self):
        assert slugify("###") == "client"


class TestWidgetSnippet:
    def _snippet(self):
        return _widget_snippet(
            tenant_slug="bella-vista-realty",
            api_base="https://api.example.com",
            brand_name="Bella Vista Realty",
            primary_color="#9B6B43",
            title="Chat with us",
            widget_key="widget-key-abc123",
        )

    def test_embeds_tenant_slug(self):
        assert "bella-vista-realty" in self._snippet()

    def test_embeds_api_base(self):
        assert "https://api.example.com" in self._snippet()

    def test_embeds_brand_color(self):
        snippet = self._snippet()
        assert "#9B6B43" in snippet
        assert "Bella Vista Realty" in snippet

    def test_uses_webhook_endpoints(self):
        snippet = self._snippet()
        assert "/webhook/message" in snippet
        assert "/webhook/start" in snippet

    def test_embeds_widget_key_header(self):
        snippet = self._snippet()
        assert "widget-key-abc123" in snippet
        assert "X-Widget-Key" in snippet

    def test_widget_key_absent_no_header(self):
        snippet = _widget_snippet(
            tenant_slug="x", api_base="", brand_name="", primary_color="",
            title=DEFAULT_WIDGET_TITLE, widget_key="",
        )
        assert "X-Widget-Key" not in snippet

    def test_default_title(self):
        snippet = _widget_snippet(
            tenant_slug="x", api_base="", brand_name="", primary_color="", title=DEFAULT_WIDGET_TITLE
        )
        assert DEFAULT_WIDGET_TITLE in snippet

    # ── FIX 3: widget must detect backend failures ─────────────────────────
    # send()/start() used to swallow non-2xx responses: a 500 with a JSON
    # error body was treated as a successful reply, so site visitors never
    # knew the assistant was down. Now both check ``r.ok`` and render a
    # distinct fallback message + console.error instead.

    def test_send_checks_r_ok(self):
        snippet = self._snippet()
        assert "if (!r.ok) throw new Error('message endpoint HTTP ' + r.status);" in snippet

    def test_start_checks_r_ok(self):
        snippet = self._snippet()
        assert "if (!r.ok) throw new Error('token endpoint HTTP ' + r.status);" in snippet
        assert "if (!s.ok) throw new Error('start endpoint HTTP ' + s.status);" in snippet

    def test_backend_failure_shows_distinct_fallback(self):
        snippet = self._snippet()
        assert "BACKEND_ERROR" in snippet
        assert "something went wrong on our end" in snippet

    def test_backend_failure_logged_to_console(self):
        snippet = self._snippet()
        assert "console.error('[LeadPulse] send failed: ' + (err && err.message));" in snippet
        assert "console.error('[LeadPulse] start failed: ' + (err && err.message));" in snippet


class TestChecklist:
    def _checklist(self):
        return _setup_checklist(
            org={
                "name": "Bella Vista Realty",
                "slug": "bella-vista-realty",
                "id": "abc-123",
                "plan_tier": "starter",
                "widget_path": None,
            },
            admin_email="owner@bv.com",
            admin_password="temp-pass",
            api_base="https://api.example.com",
            app_hostname="app.leadpulse.ai",
            vertical="real_estate",
        )

    def test_includes_org_details(self):
        checklist = self._checklist()
        assert "Bella Vista Realty" in checklist
        assert "bella-vista-realty" in checklist
        assert "starter" in checklist

    def test_includes_admin_credentials(self):
        checklist = self._checklist()
        assert "owner@bv.com" in checklist
        assert "temp-pass" in checklist

    def test_includes_subdomain_cname(self):
        assert "bella-vista-realty.app.leadpulse.ai" in self._checklist()

    def test_missing_admin_notes_manual_step(self):
        checklist = _setup_checklist(
            org={"name": "X", "slug": "x", "id": "1", "plan_tier": "starter", "widget_path": None},
            admin_email=None,
            admin_password=None,
            api_base="https://api.example.com",
            app_hostname=None,
            vertical="generic",
        )
        assert "--admin-email" in checklist

    def test_includes_widget_key_step(self):
        checklist = _setup_checklist(
            org={"name": "X", "slug": "x", "id": "1", "plan_tier": "starter", "widget_path": None},
            admin_email=None,
            admin_password=None,
            api_base="https://api.example.com",
            app_hostname=None,
            vertical="generic",
        )
        assert "X-Widget-Key" in checklist
        assert "no Host/CNAME setup required" in checklist

    def test_includes_notification_phone_when_present(self):
        checklist = _setup_checklist(
            org={
                "name": "X", "slug": "x", "id": "1", "plan_tier": "starter",
                "widget_path": None, "notification_phone": "+15550123",
            },
            admin_email=None,
            admin_password=None,
            api_base="https://api.example.com",
            app_hostname=None,
            vertical="generic",
        )
        assert "+15550123" in checklist

    def test_includes_hot_lead_email_section(self):
        checklist = _setup_checklist(
            org={"name": "X", "slug": "x", "id": "1", "plan_tier": "starter", "widget_path": None},
            admin_email="owner@x.com",
            admin_password="p",
            api_base="https://api.example.com",
            app_hostname=None,
            vertical="generic",
        )
        assert "RESEND_API_KEY" in checklist
        assert "SMS_ENABLED=true" in checklist


class TestParseArgs:
    def test_requires_vertical_choice(self):
        assert VALID_VERTICALS == ("generic", "real_estate", "insurance")

    def test_valid_vertical_accepted(self):
        args = parse_args([
            "--agency", "Acme", "--vertical", "real_estate", "--gemini-key", "k",
        ])
        assert args.agency == "Acme"
        assert args.vertical == "real_estate"
        assert args.plan_tier == "starter"

    def test_invalid_vertical_rejected(self):
        with pytest.raises(SystemExit):
            parse_args(["--agency", "Acme", "--vertical", "nonsense", "--gemini-key", "k"])
