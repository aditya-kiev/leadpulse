"""Hot-lead / meeting-booked notifications to the tenant's admin.

Every notification is tenant-scoped: the recipient is resolved from the
``users`` table (the org_admin row created at onboarding) for email, and from
the organizations record (``notification_phone``) for SMS. No global or
hardcoded addresses are ever used.
"""

import logging
from uuid import UUID

from app.agent.tools.email import send_email
from app.agent.tools.sms import send_sms
from app.config.settings import settings
from app.database.crud import get_organization_by_id
from app.database.models import User
from app.database.session import async_session_factory
from sqlalchemy import select

logger = logging.getLogger(__name__)


async def get_org_admin_email(tenant_id: UUID) -> str | None:
    """Resolve the org_admin's email for a tenant (created at onboarding)."""
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(User.email)
                .where(User.organization_id == tenant_id, User.role == "org_admin")
                .order_by(User.created_at.asc())
                .limit(1)
            )
            return result.scalar_one_or_none()
    except Exception as e:
        logger.warning("get_org_admin_email failed tenant=%s: %s", tenant_id, e)
        return None


async def get_org_notification_phone(tenant_id: UUID) -> str | None:
    """Resolve the tenant's notification phone (set at onboarding)."""
    try:
        async with async_session_factory() as session:
            org = await get_organization_by_id(session, tenant_id)
            return org.notification_phone if org else None
    except Exception as e:
        logger.warning("get_org_notification_phone failed tenant=%s: %s", tenant_id, e)
        return None


def password_reset_url(token: str) -> str:
    """Build the dashboard reset link. Falls back to app_hostname, else a
    token-only form the operator can paste into the reset screen."""
    host = (settings.app_hostname or "").strip()
    if host:
        host = host if host.startswith(("http://", "https://")) else f"https://{host}"
        return f"{host.rstrip('/')}/auth/reset?token={token}"
    return token


async def send_password_reset_email(to: str, token: str) -> dict:
    """Email a password-reset link to the requesting user.

    Reuses the existing Resend/local-stub ``send_email`` path — no new email
    dependency. The token is single-use and short-lived (see auth service).
    """
    reset_url = password_reset_url(token)
    subject = "Reset your LeadPulse password"
    if reset_url == token:
        body = (
            f"Use this one-time code to reset your LeadPulse password:\n\n"
            f"{token}\n\n"
            f"It expires in {settings.password_reset_token_ttl_minutes} minutes. "
            f"If you didn't request this, you can safely ignore this email."
        )
    else:
        body = (
            f"Click the link below to reset your LeadPulse password. It is valid "
            f"for {settings.password_reset_token_ttl_minutes} minutes.\n\n"
            f"{reset_url}\n\n"
            f"If you didn't request this, you can safely ignore this email."
        )
    return await send_email(to, subject, body)


def _lead_summary(state: dict) -> tuple[str, str]:
    lead_name = state.get("lead_name") or "A lead"
    company = state.get("company_name") or ""
    status = state.get("lead_status") or "unknown"
    meeting = state.get("meeting_time")
    subject = "Hot lead: " + lead_name if state.get("lead_status") == "hot" else "Meeting booked: " + lead_name
    lines = [
        f"Lead: {lead_name}" + (f" ({company})" if company else ""),
        f"Status: {status}",
    ]
    if meeting:
        lines.append(f"Meeting: {meeting}")
    return subject, "\n".join(lines) + "\n\nReply in the LeadPulse dashboard for full context."


async def notify_tenant(
    tenant_id: UUID,
    state: dict,
    *,
    trigger_email: bool = True,
) -> None:
    """Send admin email + SMS when a lead turns hot or a meeting is booked.

    Guards against failures so a notification outage never breaks the agent.
    """
    is_hot = state.get("lead_status") == "hot"
    booked = bool(state.get("booking_confirmed"))
    if not is_hot and not booked:
        return

    subject, body = _lead_summary(state)

    # Email — always when triggered and a recipient exists.
    if trigger_email:
        admin_email = await get_org_admin_email(tenant_id)
        if admin_email:
            try:
                await send_email(admin_email, subject, body)
                logger.info("notified admin email tenant=%s hot=%s booked=%s", tenant_id, is_hot, booked)
            except Exception as e:
                logger.warning("send_email failed tenant=%s: %s", tenant_id, e)
        else:
            logger.info("no org_admin email for tenant=%s — email notification skipped", tenant_id)

    # SMS — only when the tenant (via global SMS_ENABLED) permits it.
    if settings.sms_enabled:
        phone = await get_org_notification_phone(tenant_id)
        if phone:
            try:
                body_sms = f"{subject}. {body.splitlines()[0]}"
                await send_sms(phone, body_sms)
                logger.info("notified admin sms tenant=%s hot=%s booked=%s", tenant_id, is_hot, booked)
            except Exception as e:
                logger.warning("send_sms failed tenant=%s: %s", tenant_id, e)
        else:
            logger.info("no notification_phone for tenant=%s — sms notification skipped", tenant_id)