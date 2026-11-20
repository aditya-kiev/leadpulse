"""Weekly value report emailed to each client.

Clients rarely cancel because a tool broke; they cancel because they stopped
*seeing* it work.  Every Monday this emails the org admin a plain summary:
conversations captured, how many were hot/warm/cold, how many left a phone or
email, meetings requested, handoff requests, and the hottest leads with their
contact details, so the value is visible without logging in.

Run from cron:  python -m scripts.send_weekly_reports
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.agent.tools.email import send_email
from app.database.models import LeadConversation, Organization
from app.database.session import async_session_factory
from app.services.billing import is_billing_current
from app.services.notifications import get_org_admin_email

logger = logging.getLogger(__name__)

TOP_LEADS = 5


async def build_weekly_report(
    tenant_id: UUID,
    since: datetime,
    until: datetime,
    session_factory=None,
) -> dict[str, Any]:
    factory = session_factory or async_session_factory
    async with factory() as db:
        rows = (
            await db.execute(
                select(LeadConversation).where(
                    LeadConversation.tenant_id == tenant_id,
                    LeadConversation.created_at >= since,
                    LeadConversation.created_at < until,
                )
            )
        ).scalars().all()

    by_status = {"hot": 0, "warm": 0, "cold": 0, "unscored": 0}
    for r in rows:
        by_status[r.lead_status if r.lead_status in by_status else "unscored"] += 1

    def _has_contact(r) -> bool:
        return bool(r.lead_email or r.lead_phone)

    hottest = sorted(
        (r for r in rows if r.lead_status in ("hot", "warm")),
        key=lambda r: (r.lead_status != "hot", -(r.qualification_score or 0)),
    )[:TOP_LEADS]

    return {
        "since": since,
        "until": until,
        "conversations": len(rows),
        "by_status": by_status,
        "with_contact": sum(1 for r in rows if _has_contact(r)),
        "meetings": sum(1 for r in rows if r.booking_confirmed),
        "handoffs": sum(1 for r in rows if r.human_escalated),
        "top_leads": [
            {
                "name": r.lead_name or "Unnamed lead",
                "company": r.company_name,
                "status": r.lead_status,
                "phone": r.lead_phone,
                "email": r.lead_email,
                "budget": r.budget,
                "timeline": r.timeline,
                "meeting_time": r.meeting_time.isoformat() if r.meeting_time else None,
            }
            for r in hottest
        ],
    }


def render_report_email(business: str, report: dict[str, Any]) -> tuple[str, str]:
    s, e = report["since"], report["until"]
    st = report["by_status"]
    subject = f"{business}: your weekly lead report ({s:%b %d} - {e:%b %d})"
    if report["conversations"] == 0:
        body = (
            f"No new website conversations between {s:%b %d} and {e:%b %d}.\n\n"
            "If you expected visitors, please confirm the chat widget is still installed on your "
            "site and reply to this email so we can check it for you."
        )
        return subject, body

    lines = [
        f"Here's what your assistant did between {s:%b %d} and {e:%b %d}:",
        "",
        f"  Conversations started:        {report['conversations']}",
        f"  Hot / warm / cold:            {st['hot']} / {st['warm']} / {st['cold']}",
        f"  Left a phone or email:        {report['with_contact']}",
        f"  Meetings requested:           {report['meetings']}",
        f"  Asked for a person:           {report['handoffs']}",
    ]
    if report["top_leads"]:
        lines += ["", "Leads worth calling first:"]
        for i, lead in enumerate(report["top_leads"], 1):
            who = lead["name"] + (f" ({lead['company']})" if lead["company"] else "")
            contact = " | ".join(x for x in (lead["phone"], lead["email"]) if x) or "no contact left"
            extra = ", ".join(
                x for x in (
                    f"budget {lead['budget']:,.0f}" if lead["budget"] else None,
                    lead["timeline"],
                    f"meeting {lead['meeting_time']}" if lead["meeting_time"] else None,
                ) if x
            )
            lines.append(f"  {i}. {who} [{(lead['status'] or '').upper()}]: {contact}" + (f"  - {extra}" if extra else ""))
    lines += ["", "Reply to this email with any questions or changes you'd like to the assistant."]
    return subject, "\n".join(lines)


async def send_weekly_reports(now: datetime | None = None, session_factory=None) -> dict[str, int]:
    """Email every active, paid-up client their last-7-days report."""
    until = (now or datetime.utcnow()).replace(minute=0, second=0, microsecond=0)
    since = until - timedelta(days=7)
    factory = session_factory or async_session_factory
    sent = skipped = failed = 0

    async with factory() as db:
        orgs = (await db.execute(select(Organization).where(Organization.is_active.is_(True)))).scalars().all()

    for org in orgs:
        if not is_billing_current(org):
            skipped += 1
            continue
        try:
            to = await get_org_admin_email(org.id)
            if not to:
                skipped += 1
                continue
            report = await build_weekly_report(org.id, since, until, session_factory=factory)
            subject, body = render_report_email(org.brand_name or org.name, report)
            await send_email(to, subject, body)
            sent += 1
        except Exception:
            failed += 1
            logger.exception("weekly report failed tenant=%s", org.id)
    return {"sent": sent, "skipped": skipped, "failed": failed}
