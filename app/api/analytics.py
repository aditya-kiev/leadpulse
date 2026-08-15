import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.database.session import async_session_factory
from app.database.crud import get_daily_summaries, get_organization_by_id
from app.models.schemas import ConversationHistoryOut
from app.services.analytics import compute_org_metrics
from app.services.branding import get_branding, apply_branding_to_html

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])


class LeadVolumeMetric(BaseModel):
    total: int
    hot: int
    warm: int
    cold: int


class FunnelMetric(BaseModel):
    total: int
    greeting: int
    collecting: int
    qualified: int


class OrgMetricsOut(BaseModel):
    lead_volume: LeadVolumeMetric
    qualification_rate: float
    booking_rate: float
    funnel: FunnelMetric
    average_qualification_score: float | None = None
    meetings_booked: int
    human_escalations: int
    average_response_time_seconds: float | None = None
    cost_per_booked_meeting: float | None = None


class DailySummaryOut(BaseModel):
    date: str
    total_conversations: int
    qualified_leads: int
    hot_leads: int
    warm_leads: int
    cold_leads: int
    meetings_booked: int
    human_escalations: int
    avg_qualification_score: float | None = None
    total_cost: float = 0.0


@router.get("/metrics", response_model=OrgMetricsOut)
async def get_org_metrics(
    request: Request,
    start_date: str = Query(default="", description="YYYY-MM-DD"),
    end_date: str = Query(default="", description="YYYY-MM-DD"),
    _auth: tuple = Depends(get_current_user),
):
    tenant_id: UUID | None = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="Tenant context required")

    sd = start_date or (datetime.now(timezone.utc).replace(day=1).strftime("%Y-%m-%d"))
    ed = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return await compute_org_metrics(tenant_id, start_date=sd, end_date=ed)


@router.get("/daily", response_model=list[DailySummaryOut])
async def get_daily_summaries_endpoint(
    request: Request,
    start_date: str = Query(default="", description="YYYY-MM-DD"),
    end_date: str = Query(default="", description="YYYY-MM-DD"),
    _auth: tuple = Depends(get_current_user),
):
    tenant_id: UUID | None = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="Tenant context required")

    sd = start_date or (datetime.now(timezone.utc).replace(day=1).strftime("%Y-%m-%d"))
    ed = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with async_session_factory() as session:
        rows = await get_daily_summaries(session, tenant_id, start_date=sd, end_date=ed)
        return [
            DailySummaryOut(
                date=r.date,
                total_conversations=r.total_conversations,
                qualified_leads=r.qualified_leads,
                hot_leads=r.hot_leads,
                warm_leads=r.warm_leads,
                cold_leads=r.cold_leads,
                meetings_booked=r.meetings_booked,
                human_escalations=r.human_escalations,
                avg_qualification_score=r.avg_qualification_score,
                total_cost=r.total_cost,
            )
            for r in rows
        ]


_DASHBOARD_HTML: str | None = None


@router.get("/dashboard", response_class=HTMLResponse)
async def analytics_dashboard(request: Request):
    global _DASHBOARD_HTML
    if _DASHBOARD_HTML is None:
        import os
        html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        _DASHBOARD_HTML = open(html_path, encoding="utf-8").read()

    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id:
        try:
            async with async_session_factory() as session:
                org = await get_organization_by_id(session, tenant_id)
                if org:
                    b = get_branding(
                        brand_name=org.brand_name,
                        logo_url=org.logo_url,
                        primary_color=org.primary_color,
                        custom_domain=org.custom_domain,
                    )
                    return HTMLResponse(apply_branding_to_html(_DASHBOARD_HTML, b))
        except Exception:
            pass
    return HTMLResponse(_DASHBOARD_HTML)
