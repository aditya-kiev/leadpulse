"""Once-only client alerts and CRM sync, dispatched after a turn is saved.

Why this exists
---------------
Alerts and CRM pushes used to live in the ``end`` graph node.  That node:

* was never reached by a hot lead who had not booked yet (no alert at all), and
* re-ran on every message after a conversation ended (duplicate CRM pushes and
  duplicate emails to the client on every "thanks!").

``dispatch_lead_events`` runs after ``save_state`` (as a FastAPI background
task, so the visitor never waits on email / CRM retries) and uses atomic
``UPDATE ... WHERE marker IS NULL RETURNING`` claims, so each side effect
happens at most once even with concurrent requests or multiple workers.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import update

from app.agent.tools.crm import update_crm
from app.database.models import LeadConversation
from app.database.session import async_session_factory
from app.services.notifications import notify_tenant

logger = logging.getLogger(__name__)

CRM_FIELDS = (
    "lead_name", "company_name", "industry", "budget", "timeline",
    "problem_statement", "lead_email", "lead_phone", "qualification_score",
    "lead_status", "lead_type", "meeting_time", "booking_confirmed", "human_escalated",
)


def _tenant_clause(tenant_id: UUID | None):
    return LeadConversation.tenant_id.is_(None) if tenant_id is None else LeadConversation.tenant_id == tenant_id


async def _claim_marker(session_id: str, tenant_id: UUID | None, column: str) -> bool:
    """Atomically set ``column`` if it is still NULL.  True only for the winner."""
    col = getattr(LeadConversation, column)
    stmt = (
        update(LeadConversation)
        .where(LeadConversation.session_id == session_id, _tenant_clause(tenant_id), col.is_(None))
        .values({column: datetime.utcnow()})
        .returning(LeadConversation.id)
    )
    async with async_session_factory() as db:
        result = await db.execute(stmt)
        await db.commit()
        return result.scalar_one_or_none() is not None


def crm_lead_data(state: dict) -> dict[str, Any]:
    return {k: state.get(k) for k in CRM_FIELDS}


def crm_fingerprint(lead_data: dict[str, Any]) -> str:
    payload = json.dumps(lead_data, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


async def _claim_crm_sync(session_id: str, tenant_id: UUID | None, fingerprint: str) -> bool:
    """Compare-and-set: True only if the CRM-relevant data changed since the last push."""
    stmt = (
        update(LeadConversation)
        .where(
            LeadConversation.session_id == session_id,
            _tenant_clause(tenant_id),
            LeadConversation.crm_sync_hash.is_distinct_from(fingerprint),
        )
        .values(crm_sync_hash=fingerprint)
        .returning(LeadConversation.id)
    )
    async with async_session_factory() as db:
        result = await db.execute(stmt)
        await db.commit()
        return result.scalar_one_or_none() is not None


async def dispatch_lead_events(session_id: str, tenant_id: UUID | None, state: dict) -> dict[str, Any]:
    """Send each alert / CRM push at most once.  Never raises."""
    outcome: dict[str, Any] = {"alerts": [], "crm": None}
    if tenant_id is None:
        outcome["skipped"] = "no_tenant"
        return outcome

    try:
        hot = state.get("lead_status") == "hot"
        booked = bool(state.get("booking_confirmed"))
        handoff = bool(state.get("human_escalated"))

        hot_won = hot and await _claim_marker(session_id, tenant_id, "hot_alert_sent_at")
        booked_won = booked and await _claim_marker(session_id, tenant_id, "booking_alert_sent_at")
        handoff_won = handoff and await _claim_marker(session_id, tenant_id, "handoff_alert_sent_at")

        # One email per turn for hot/booked: "booked" carries everything "hot" does.
        primary = "booked" if booked_won else "hot" if hot_won else None
        for event in (primary, "handoff" if handoff_won else None):
            if event:
                await notify_tenant(tenant_id, state, event=event)
                outcome["alerts"].append(event)
    except Exception:
        logger.exception("alert dispatch failed session=%s", session_id)

    try:
        if state.get("lead_status") is not None:
            lead_data = crm_lead_data(state)
            if await _claim_crm_sync(session_id, tenant_id, crm_fingerprint(lead_data)):
                outcome["crm"] = await update_crm(session_id, lead_data, tenant_id=tenant_id)
            else:
                outcome["crm"] = {"status": "unchanged"}
    except Exception:
        logger.exception("CRM dispatch failed session=%s", session_id)

    return outcome
