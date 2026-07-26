"""
Analytics aggregation layer for Phase 4 — ROI Reporting.

Computes per-org and per-tenant metrics from raw conversation data.
Pre-computed daily rollups live in the daily_org_summaries table.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, func

from app.database.crud import get_conversations_by_tenant
from app.database.models import LeadConversation, UsageLog
from app.database.session import async_session_factory

logger = logging.getLogger(__name__)


async def compute_org_metrics(
    tenant_id: UUID,
    start_date: str | None = None,
    end_date: str | None = None,
):
    """Compute the 8 core metrics for an organization over a date range."""
    async with async_session_factory() as session:
        base = select(LeadConversation).where(LeadConversation.tenant_id == tenant_id)
        if start_date:
            base = base.where(LeadConversation.created_at >= start_date)
        if end_date:
            base = base.where(LeadConversation.created_at <= end_date + "T23:59:59")

        result = await session.execute(base)
        conversations = list(result.scalars().all())

    total = len(conversations)
    qualified = [c for c in conversations if c.lead_status in ("hot", "warm")]
    hot = [c for c in conversations if c.lead_status == "hot"]
    warm = [c for c in conversations if c.lead_status == "warm"]
    cold = [c for c in conversations if c.lead_status == "cold"]
    booked = [c for c in conversations if c.booking_confirmed]
    escalated = [c for c in conversations if c.human_escalated]
    scored = [c for c in conversations if c.qualification_score is not None]

    qual_rate = len(qualified) / total if total else 0
    booking_rate = len(booked) / total if total else 0
    avg_score = sum(c.qualification_score for c in scored) / len(scored) if scored else 0

    funnel = {
        "total": total,
        "greeting": len([c for c in conversations if c.conversation_stage == "greeting"]),
        "collecting": len([c for c in conversations if c.conversation_stage == "collecting"]),
        "qualified": len([c for c in conversations if c.conversation_stage == "qualified"]),
    }

    # Average response time — per-message timestamps are not stored in
    # conversation_history for existing conversations, so this is None
    # until timestamps are collected going forward (see graph.py where
    # messages now get a "timestamp" field appended on each turn).
    average_response_time_seconds = None

    # Cost-per-booked-meeting: total Gemini cost over period / meetings_booked
    # "Cost" here = Gemini API inference cost only (estimated from token
    # counts at per-token rates from app/agent/gemini.py).
    total_cost = 0.0
    cost_per_booked_meeting = None
    if total > 0:
        async with async_session_factory() as session:
            cost_query = select(func.coalesce(func.sum(UsageLog.estimated_cost), 0.0)).where(
                UsageLog.organization_id == tenant_id,
            )
            if start_date:
                cost_query = cost_query.where(UsageLog.created_at >= start_date)
            if end_date:
                cost_query = cost_query.where(UsageLog.created_at <= end_date + "T23:59:59")
            cost_result = await session.execute(cost_query)
            total_cost = cost_result.scalar() or 0.0
        if len(booked) > 0:
            cost_per_booked_meeting = round(total_cost / len(booked), 4)

    return {
        "lead_volume": {
            "total": total,
            "hot": len(hot),
            "warm": len(warm),
            "cold": len(cold),
        },
        "qualification_rate": round(qual_rate, 4),
        "booking_rate": round(booking_rate, 4),
        "funnel": funnel,
        "average_qualification_score": round(avg_score, 4),
        "meetings_booked": len(booked),
        "human_escalations": len(escalated),
        "average_response_time_seconds": average_response_time_seconds,
        "cost_per_booked_meeting": cost_per_booked_meeting,
        "_notes": {
            "average_response_time_seconds": (
                "Not available for conversations created before this metric was added. "
                "Per-message timestamps are now collected going forward."
            ),
            "cost_per_booked_meeting": "Gemini API inference cost only (estimated from token counts).",
        },
    }


async def run_daily_rollup() -> int:
    """Compute and store daily summaries for all organizations with activity.
    Called by a scheduled job (cron / background worker)."""
    from app.database.crud import upsert_daily_summary

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    orgs_result = await _get_active_orgs()

    updated = 0
    for org_id in orgs_result:
        metrics = await compute_org_metrics(org_id, start_date=yesterday, end_date=yesterday)
        async with async_session_factory() as session:
            await upsert_daily_summary(
                session,
                org_id,
                date=yesterday,
                total_conversations=metrics["lead_volume"]["total"],
                qualified_leads=metrics["lead_volume"]["hot"] + metrics["lead_volume"]["warm"],
                hot_leads=metrics["lead_volume"]["hot"],
                warm_leads=metrics["lead_volume"]["warm"],
                cold_leads=metrics["lead_volume"]["cold"],
                meetings_booked=metrics["meetings_booked"],
                human_escalations=metrics["human_escalations"],
                avg_qualification_score=metrics["average_qualification_score"],
            )
            await session.commit()
        updated += 1

    logger.info("Daily rollup complete: %d orgs updated for %s", updated, yesterday)
    return updated


async def _get_active_orgs() -> list[UUID]:
    from app.database.models import Organization
    async with async_session_factory() as session:
        result = await session.execute(
            select(Organization.id).where(Organization.is_active == True)
        )
        return [row[0] for row in result.all()]
