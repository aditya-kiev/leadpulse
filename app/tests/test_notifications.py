"""Hot-lead / meeting-booked notification tests (Task 2).

Verifies notify_tenant sends exactly one email and one SMS to the right
tenant-scoped recipients when a lead flips to "hot" or a meeting is booked,
and never fires for cold/warm leads or without a recipient.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from app.services.notifications import notify_tenant

TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")


def _state(lead_status="hot", booking_confirmed=False, **extra):
    state = {
        "lead_name": "Alice",
        "company_name": "Acme",
        "lead_status": lead_status,
        "booking_confirmed": booking_confirmed,
        "meeting_time": None,
    }
    state.update(extra)
    return state


@pytest.mark.asyncio
async def test_hot_lead_notifies_email_once():
    with patch("app.services.notifications.get_org_admin_email", new_callable=AsyncMock, return_value="owner@acme.com"), \
         patch("app.services.notifications.get_org_notification_phone", new_callable=AsyncMock, return_value=None), \
         patch("app.services.notifications.settings.sms_enabled", False), \
         patch("app.services.notifications.send_email", new_callable=AsyncMock) as mock_email:
        await notify_tenant(TENANT_ID, _state(lead_status="hot"))

        mock_email.assert_awaited_once_with("owner@acme.com", "Hot lead: Alice", mock_email.await_args.args[2])


@pytest.mark.asyncio
async def test_hot_lead_sms_when_enabled():
    with patch("app.services.notifications.get_org_admin_email", new_callable=AsyncMock, return_value=None), \
         patch("app.services.notifications.get_org_notification_phone", new_callable=AsyncMock, return_value="+15550123"), \
         patch("app.services.notifications.settings.sms_enabled", True), \
         patch("app.services.notifications.send_sms", new_callable=AsyncMock) as mock_sms:
        await notify_tenant(TENANT_ID, _state(lead_status="hot"))

        mock_sms.assert_awaited_once()
        assert mock_sms.await_args.args[0] == "+15550123"


@pytest.mark.asyncio
async def test_meeting_booked_notifies_email_even_if_warm():
    with patch("app.services.notifications.get_org_admin_email", new_callable=AsyncMock, return_value="owner@acme.com"), \
         patch("app.services.notifications.get_org_notification_phone", new_callable=AsyncMock, return_value=None), \
         patch("app.services.notifications.settings.sms_enabled", False), \
         patch("app.services.notifications.send_email", new_callable=AsyncMock) as mock_email:
        await notify_tenant(TENANT_ID, _state(lead_status="warm", booking_confirmed=True))

        args = mock_email.await_args.args
        assert mock_email.await_count == 1
        assert "Meeting booked" in args[1]
        assert args[0] == "owner@acme.com"


@pytest.mark.asyncio
async def test_cold_lead_no_notification():
    with patch("app.services.notifications.get_org_admin_email", new_callable=AsyncMock, return_value="owner@acme.com"), \
         patch("app.services.notifications.get_org_notification_phone", new_callable=AsyncMock, return_value="+15550123"), \
         patch("app.services.notifications.settings.sms_enabled", True), \
         patch("app.services.notifications.send_email", new_callable=AsyncMock) as mock_email, \
         patch("app.services.notifications.send_sms", new_callable=AsyncMock) as mock_sms:
        await notify_tenant(TENANT_ID, _state(lead_status="cold"))

        mock_email.assert_not_awaited()
        mock_sms.assert_not_awaited()


@pytest.mark.asyncio
async def test_warm_lead_no_booking_no_notification():
    with patch("app.services.notifications.get_org_admin_email", new_callable=AsyncMock, return_value="owner@acme.com"), \
         patch("app.services.notifications.get_org_notification_phone", new_callable=AsyncMock, return_value="+15550123"), \
         patch("app.services.notifications.settings.sms_enabled", True), \
         patch("app.services.notifications.send_email", new_callable=AsyncMock) as mock_email, \
         patch("app.services.notifications.send_sms", new_callable=AsyncMock) as mock_sms:
        await notify_tenant(TENANT_ID, _state(lead_status="warm", booking_confirmed=False))

        mock_email.assert_not_awaited()
        mock_sms.assert_not_awaited()


@pytest.mark.asyncio
async def test_hot_but_no_recipient_no_calls():
    with patch("app.services.notifications.get_org_admin_email", new_callable=AsyncMock, return_value=None), \
         patch("app.services.notifications.get_org_notification_phone", new_callable=AsyncMock, return_value=None), \
         patch("app.services.notifications.settings.sms_enabled", True), \
         patch("app.services.notifications.send_email", new_callable=AsyncMock) as mock_email, \
         patch("app.services.notifications.send_sms", new_callable=AsyncMock) as mock_sms:
        await notify_tenant(TENANT_ID, _state(lead_status="hot"))

        mock_email.assert_not_awaited()
        mock_sms.assert_not_awaited()


# ── send_email provider (Resend) ─────────────────────────────────────────

from app.agent.tools.email import send_email, get_email_log  # noqa: E402


@pytest.mark.asyncio
async def test_email_stub_fallback_without_key():
    from app.config.settings import settings
    with patch.object(settings, "resend_api_key", ""):
        result = await send_email("admin@acme.com", "Hot lead", "Hello")

    assert result["status"] == "sent"
    assert result["id"].startswith("stub-")
    assert result["to"] == "admin@acme.com"
    assert result["subject"] == "Hot lead"
    assert any(e["id"] == result["id"] for e in get_email_log())


@pytest.mark.asyncio
async def test_email_calls_resend_when_configured():
    from app.config.settings import settings

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": "re_123"}
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp

    with patch.object(settings, "resend_api_key", "re_test_key"), \
         patch.object(settings, "resend_from_email", "leads@acme.com"), \
         patch("httpx.post", return_value=mock_resp):
        result = await send_email("admin@acme.com", "Hot lead", "Hello")

    assert result["id"] == "re_123"
    assert result["status"] == "sent"
    assert result["to"] == "admin@acme.com"