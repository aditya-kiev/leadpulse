"""Once-only alerts + CRM sync, against REAL Postgres.

Regression tests for the original bugs:
  * a hot lead who had not booked yet never triggered an alert / CRM push
  * every message after the conversation ended re-pushed the lead and
    re-emailed the client
"""
import asyncio
import os
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import LeadConversation, Organization
from app.services import alerts
from app.services.alerts import crm_fingerprint, crm_lead_data, dispatch_lead_events
from app.services.notifications import _lead_summary, calendar_link, infer_event

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


HOT = {
    "lead_name": "Alice Smith", "company_name": "Acme", "industry": "insurance",
    "budget": 50000.0, "timeline": "this month", "problem_statement": "auto + home bundle",
    "lead_email": "alice@example.com", "lead_phone": "4155550123",
    "qualification_score": 0.86, "lead_status": "hot", "lead_type": "company",
    "booking_confirmed": False, "meeting_time": None, "human_escalated": False,
}


@pytest.fixture
async def tenant_and_session(pg_session_factory):
    org_id, sid = None, f"alerts-{uuid4().hex[:10]}"
    async with pg_session_factory() as db:
        org = Organization(
            name="Alerts Org", slug=f"alerts-{uuid4().hex[:8]}", brand_name="Alerts",
            logo_url="", primary_color="#000000", custom_domain="",
            custom_domain_status="unverified", tls_status="none",
            domain_verification_token=None, widget_key=f"wk-{uuid4().hex[:10]}",
        )
        db.add(org)
        await db.flush()
        org_id = org.id
        db.add(LeadConversation(session_id=sid, tenant_id=org_id))
        await db.commit()
    with patch("app.services.alerts.async_session_factory", pg_session_factory), \
         patch("app.services.alerts.notify_tenant", new_callable=AsyncMock) as notify, \
         patch("app.services.alerts.update_crm", new_callable=AsyncMock, return_value={"status": "ok"}) as crm:
        yield org_id, sid, notify, crm, pg_session_factory
    async with pg_session_factory() as db:
        await db.execute(delete(LeadConversation).where(LeadConversation.tenant_id == org_id))
        await db.execute(delete(Organization).where(Organization.id == org_id))
        await db.commit()


@pytest.mark.asyncio
async def test_hot_unbooked_lead_alerts_immediately_exactly_once(tenant_and_session):
    org_id, sid, notify, crm, _ = tenant_and_session
    for _ in range(4):  # four more messages from the same lead
        await dispatch_lead_events(sid, org_id, dict(HOT))
    assert notify.await_count == 1
    assert notify.await_args.kwargs["event"] == "hot"
    assert crm.await_count == 1  # pushed once, data never changed


@pytest.mark.asyncio
async def test_booking_sends_one_more_alert_and_one_crm_update(tenant_and_session):
    org_id, sid, notify, crm, _ = tenant_and_session
    await dispatch_lead_events(sid, org_id, dict(HOT))
    booked = {**HOT, "booking_confirmed": True, "meeting_time": "2026-10-28T10:00:00+00:00"}
    for _ in range(5):  # "thanks!", "great", ... after the conversation ended
        await dispatch_lead_events(sid, org_id, dict(booked))
    assert [c.kwargs["event"] for c in notify.await_args_list] == ["hot", "booked"]
    assert crm.await_count == 2  # initial qualification + booking update


@pytest.mark.asyncio
async def test_hot_and_booked_in_same_turn_sends_single_booked_email(tenant_and_session):
    org_id, sid, notify, crm, _ = tenant_and_session
    state = {**HOT, "booking_confirmed": True, "meeting_time": "2026-10-28T10:00:00+00:00"}
    await dispatch_lead_events(sid, org_id, state)
    assert [c.kwargs["event"] for c in notify.await_args_list] == ["booked"]


@pytest.mark.asyncio
async def test_handoff_request_alerts_even_for_a_cold_lead(tenant_and_session):
    org_id, sid, notify, crm, _ = tenant_and_session
    cold = {**HOT, "lead_status": "cold", "human_escalated": True}
    await dispatch_lead_events(sid, org_id, cold)
    await dispatch_lead_events(sid, org_id, cold)
    assert [c.kwargs["event"] for c in notify.await_args_list] == ["handoff"]


@pytest.mark.asyncio
async def test_new_contact_details_trigger_exactly_one_crm_resync(tenant_and_session):
    org_id, sid, notify, crm, _ = tenant_and_session
    no_contact = {**HOT, "lead_email": None, "lead_phone": None}
    await dispatch_lead_events(sid, org_id, no_contact)
    await dispatch_lead_events(sid, org_id, no_contact)
    assert crm.await_count == 1
    await dispatch_lead_events(sid, org_id, dict(HOT))
    await dispatch_lead_events(sid, org_id, dict(HOT))
    assert crm.await_count == 2
    pushed = crm.await_args_list[-1].args[1]
    assert pushed["lead_phone"] == "4155550123" and pushed["lead_email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_unqualified_lead_triggers_nothing(tenant_and_session):
    org_id, sid, notify, crm, _ = tenant_and_session
    await dispatch_lead_events(sid, org_id, {"lead_name": "Bob", "lead_status": None})
    notify.assert_not_called()
    crm.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_dispatches_claim_each_side_effect_once(tenant_and_session):
    """Several workers / requests finishing at once must still send ONE email."""
    org_id, sid, notify, crm, _ = tenant_and_session
    await asyncio.gather(*(dispatch_lead_events(sid, org_id, dict(HOT)) for _ in range(8)))
    assert notify.await_count == 1
    assert crm.await_count == 1


@pytest.mark.asyncio
async def test_markers_are_persisted_on_the_row(tenant_and_session):
    org_id, sid, notify, crm, factory = tenant_and_session
    await dispatch_lead_events(sid, org_id, dict(HOT))
    async with factory() as db:
        row = (await db.execute(select(LeadConversation).where(LeadConversation.session_id == sid))).scalar_one()
    assert row.hot_alert_sent_at is not None
    assert row.booking_alert_sent_at is None
    assert row.crm_sync_hash == crm_fingerprint(crm_lead_data(HOT))


@pytest.mark.asyncio
async def test_single_tenant_mode_is_skipped():
    out = await dispatch_lead_events("s", None, dict(HOT))
    assert out["skipped"] == "no_tenant"


@pytest.mark.asyncio
async def test_dispatch_never_raises_when_notification_fails(tenant_and_session):
    org_id, sid, notify, crm, _ = tenant_and_session
    notify.side_effect = RuntimeError("smtp down")
    out = await dispatch_lead_events(sid, org_id, dict(HOT))
    assert out["crm"] == {"status": "ok"}  # CRM still synced


# ---- notification content (pure functions) --------------------------------

def test_alert_email_contains_contact_details_and_calendar_link():
    state = {**HOT, "booking_confirmed": True, "meeting_time": "2026-10-28T10:00:00+00:00"}
    subject, body = _lead_summary(state, "booked")
    assert subject == "Meeting booked: Alice Smith"
    assert "4155550123" in body and "alice@example.com" in body
    assert "Budget: 50,000" in body and "this month" in body
    link = calendar_link(state)
    assert link and link.startswith("https://calendar.google.com/calendar/render?")
    assert "20261028T100000Z%2F20261028T103000Z" in link
    assert link in body


def test_alert_email_says_so_when_no_contact_was_given():
    _, declined = _lead_summary({**HOT, "lead_email": None, "lead_phone": None, "contact_declined": True}, "hot")
    assert "chose not to share" in declined
    _, none = _lead_summary({**HOT, "lead_email": None, "lead_phone": None}, "hot")
    assert "not collected" in none


def test_event_inference_and_subjects():
    assert infer_event({"lead_status": "hot"}) == "hot"
    assert infer_event({"lead_status": "warm", "booking_confirmed": True}) == "booked"
    assert infer_event({"lead_status": "cold", "human_escalated": True}) == "handoff"
    assert infer_event({"lead_status": "cold"}) is None
    assert _lead_summary({"lead_name": "Zed", "human_escalated": True}, "handoff")[0] == "Handoff requested: Zed"
