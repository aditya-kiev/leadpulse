"""Weekly client value report."""
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import LeadConversation, Organization, User
from app.services.weekly_report import build_weekly_report, render_report_email, send_weekly_reports

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent_test"
)


@pytest.fixture
async def pg_session_factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _lead(org_id, **kw):
    return LeadConversation(session_id=f"wr-{uuid4().hex[:10]}", tenant_id=org_id, **kw)


@pytest.mark.asyncio
async def test_report_counts_and_email_and_billing_gate(pg_session_factory):
    now = datetime(2026, 11, 2, 9, 0)
    recent = now - timedelta(days=2)
    old = now - timedelta(days=20)
    org_ids = []
    try:
        async with pg_session_factory() as db:
            paid = Organization(name="Paid Co", slug=f"wr-{uuid4().hex[:8]}", brand_name="Paid Co")
            lapsed = Organization(name="Lapsed Co", slug=f"wr-{uuid4().hex[:8]}", billing_status="canceled")
            db.add_all([paid, lapsed])
            await db.flush()
            org_ids = [paid.id, lapsed.id]
            db.add(User(email=f"owner-{uuid4().hex[:6]}@paid.test", password_hash="x",
                        display_name="Owner", role="org_admin", organization_id=paid.id))
            db.add_all([
                _lead(paid.id, lead_name="Hot Hank", lead_status="hot", qualification_score=0.9,
                      lead_phone="4155550123", booking_confirmed=True, budget=60000.0,
                      timeline="this month", created_at=recent),
                _lead(paid.id, lead_name="Warm Wendy", lead_status="warm", lead_email="w@x.io",
                      qualification_score=0.6, created_at=recent),
                _lead(paid.id, lead_status="cold", human_escalated=True, created_at=recent),
                _lead(paid.id, lead_name="Ancient", lead_status="hot", created_at=old),  # outside window
                _lead(lapsed.id, lead_status="hot", created_at=recent),
            ])
            await db.commit()

        report = await build_weekly_report(org_ids[0], now - timedelta(days=7), now, session_factory=pg_session_factory)
        assert report["conversations"] == 3
        assert report["by_status"] == {"hot": 1, "warm": 1, "cold": 1, "unscored": 0}
        assert report["with_contact"] == 2 and report["meetings"] == 1 and report["handoffs"] == 1
        assert [l["name"] for l in report["top_leads"]] == ["Hot Hank", "Warm Wendy"]

        subject, body = render_report_email("Paid Co", report)
        assert "weekly lead report" in subject
        assert "4155550123" in body and "Hot Hank" in body and "Conversations started:        3" in body

        with patch("app.services.weekly_report.send_email", new_callable=AsyncMock) as send, \
             patch("app.services.notifications.async_session_factory", pg_session_factory):
            result = await send_weekly_reports(now=now, session_factory=pg_session_factory)
        assert result["sent"] >= 1
        recipients = [c.args[0] for c in send.await_args_list]
        assert any(r.endswith("@paid.test") for r in recipients)
        assert len(recipients) == result["sent"]  # the canceled org was skipped
    finally:
        async with pg_session_factory() as db:
            await db.execute(delete(LeadConversation).where(LeadConversation.tenant_id.in_(org_ids)))
            await db.execute(delete(User).where(User.organization_id.in_(org_ids)))
            await db.execute(delete(Organization).where(Organization.id.in_(org_ids)))
            await db.commit()


def test_empty_week_email_is_helpful():
    now = datetime(2026, 11, 2, 9, 0)
    report = {"since": now - timedelta(days=7), "until": now, "conversations": 0,
              "by_status": {}, "with_contact": 0, "meetings": 0, "handoffs": 0, "top_leads": []}
    _, body = render_report_email("Acme", report)
    assert "widget is still installed" in body
