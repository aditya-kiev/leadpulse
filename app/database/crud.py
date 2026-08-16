import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LeadConversation, Organization, User, CRMConfig, PushLog, UsageLog, DailyOrgSummary
from app.database.session import async_session_factory
from app.services.billing import is_billing_current

logger = logging.getLogger(__name__)


async def create_conversation(
    session: AsyncSession,
    session_id: str,
    tenant_id: UUID | None = None,
) -> LeadConversation:
    logger.info("CRUD create_conversation: session_id=%s tenant_id=%s", session_id, tenant_id)
    lead = LeadConversation(session_id=session_id, tenant_id=tenant_id)
    session.add(lead)
    await session.flush()
    logger.info("CRUD create_conversation: OK id=%s", lead.id)
    return lead


async def get_conversation(
    session: AsyncSession,
    session_id: str,
    tenant_id: UUID | None = None,
) -> LeadConversation | None:
    logger.info("CRUD get_conversation: session_id=%s tenant_id=%s", session_id, tenant_id)
    stmt = select(LeadConversation).where(LeadConversation.session_id == session_id)
    if tenant_id is not None:
        stmt = stmt.where(LeadConversation.tenant_id == tenant_id)
    result = await session.execute(stmt)
    lead = result.scalar_one_or_none()
    logger.info("CRUD get_conversation: found=%s", lead is not None)
    return lead


async def update_conversation(
    session: AsyncSession,
    session_id: str,
    tenant_id: UUID | None = None,
    **kwargs,
) -> LeadConversation | None:
    logger.info("CRUD update_conversation: session_id=%s tenant_id=%s kwargs=%s", session_id, tenant_id, list(kwargs.keys()))
    stmt = (
        update(LeadConversation)
        .where(LeadConversation.session_id == session_id)
        .values(**kwargs)
        .returning(LeadConversation)
    )
    if tenant_id is not None:
        stmt = stmt.where(LeadConversation.tenant_id == tenant_id)
    result = await session.execute(stmt)
    await session.commit()
    lead = result.scalar_one_or_none()
    logger.info("CRUD update_conversation: OK lead=%s", lead is not None)
    return lead


async def get_conversations_by_tenant(
    session: AsyncSession,
    tenant_id: UUID,
    limit: int = 100,
    offset: int = 0,
) -> list[LeadConversation]:
    stmt = (
        select(LeadConversation)
        .where(LeadConversation.tenant_id == tenant_id)
        .order_by(LeadConversation.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_organization_by_id(
    session: AsyncSession,
    org_id: UUID,
) -> Organization | None:
    result = await session.execute(select(Organization).where(Organization.id == org_id))
    return result.scalar_one_or_none()


async def get_organization_by_slug(
    session: AsyncSession,
    slug: str,
) -> Organization | None:
    result = await session.execute(select(Organization).where(Organization.slug == slug))
    return result.scalar_one_or_none()


async def get_organization_by_widget_key(
    session: AsyncSession,
    widget_key: str,
) -> Organization | None:
    result = await session.execute(
        select(Organization).where(
            Organization.widget_key == widget_key,
            Organization.is_active == True,
        )
    )
    org = result.scalar_one_or_none()
    if org is not None and not is_billing_current(org):
        return None
    return org


async def list_organizations(
    session: AsyncSession,
    limit: int = 100,
    offset: int = 0,
) -> list[Organization]:
    result = await session.execute(
        select(Organization).order_by(Organization.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def create_organization(
    session: AsyncSession,
    name: str,
    slug: str,
    plan_tier: str = "starter",
) -> Organization:
    org = Organization(name=name, slug=slug, plan_tier=plan_tier)
    session.add(org)
    await session.flush()
    return org


async def update_organization(
    session: AsyncSession,
    org_id: UUID,
    **kwargs,
) -> Organization | None:
    org = await get_organization_by_id(session, org_id)
    if org is None:
        return None
    for k, v in kwargs.items():
        if hasattr(org, k):
            setattr(org, k, v)
    await session.flush()
    return org


async def get_crm_config(
    session: AsyncSession,
    organization_id: UUID,
    integration_type: str | None = None,
) -> CRMConfig | None:
    stmt = select(CRMConfig).where(CRMConfig.organization_id == organization_id, CRMConfig.is_active == True)
    if integration_type:
        stmt = stmt.where(CRMConfig.integration_type == integration_type)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_users_by_organization(
    session: AsyncSession,
    organization_id: UUID,
) -> list[User]:
    result = await session.execute(
        select(User).where(User.organization_id == organization_id).order_by(User.created_at.desc())
    )
    return list(result.scalars().all())


async def log_crm_push(
    organization_id: UUID,
    integration_type: str,
    session_id: str | None = None,
    status: str = "unknown",
    attempt: int = 1,
    lead_data: dict | None = None,
    response_data: dict | None = None,
    error_message: str | None = None,
) -> PushLog:
    async with async_session_factory() as session:
        log = PushLog(
            organization_id=organization_id,
            integration_type=integration_type,
            session_id=session_id,
            status=status,
            attempt=attempt,
            lead_data=lead_data,
            response_data=response_data,
            error_message=error_message,
        )
        session.add(log)
        await session.flush()
        await session.commit()
        return log


async def log_usage(
    organization_id: UUID | None = None,
    session_id: str | None = None,
    model: str = "unknown",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    estimated_cost: float | None = None,
) -> UsageLog:
    async with async_session_factory() as session:
        log = UsageLog(
            organization_id=organization_id,
            session_id=session_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
        )
        session.add(log)
        await session.flush()
        await session.commit()
        return log


async def get_usage_logs(
    session: AsyncSession,
    organization_id: UUID,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 1000,
) -> list[UsageLog]:
    stmt = select(UsageLog).where(UsageLog.organization_id == organization_id)
    if start_date:
        stmt = stmt.where(UsageLog.created_at >= start_date)
    if end_date:
        stmt = stmt.where(UsageLog.created_at <= end_date)
    stmt = stmt.order_by(UsageLog.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def upsert_daily_summary(
    session: AsyncSession,
    organization_id: UUID,
    date: str,
    **kwargs,
) -> DailyOrgSummary:
    stmt = select(DailyOrgSummary).where(
        DailyOrgSummary.organization_id == organization_id,
        DailyOrgSummary.date == date,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        for k, v in kwargs.items():
            setattr(existing, k, v)
        await session.flush()
        return existing
    summary = DailyOrgSummary(organization_id=organization_id, date=date, **kwargs)
    session.add(summary)
    await session.flush()
    return summary


async def get_daily_summaries(
    session: AsyncSession,
    organization_id: UUID,
    start_date: str,
    end_date: str,
) -> list[DailyOrgSummary]:
    stmt = (
        select(DailyOrgSummary)
        .where(
            DailyOrgSummary.organization_id == organization_id,
            DailyOrgSummary.date >= start_date,
            DailyOrgSummary.date <= end_date,
        )
        .order_by(DailyOrgSummary.date.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_push_logs(
    session: AsyncSession,
    organization_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[PushLog]:
    stmt = (
        select(PushLog)
        .where(PushLog.organization_id == organization_id)
        .order_by(PushLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
