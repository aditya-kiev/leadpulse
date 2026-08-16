"""Billing/subscription helpers shared by the manage CLI and the Stripe webhook.

Status model (one value on ``organizations.billing_status``):

    trialing  - free trial, service fully enabled
    active    - paid, service fully enabled
    past_due  - payment failed; service stays up but is flagged
    suspended - non-payment; widget must stop serving
    canceled  - customer canceled; widget must stop serving

A subscription is "current" while it is trialing or active; everything else
blocks the widget/API via ``get_organization_by_widget_key`` (see crud.py).
"""
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Organization

BILLING_STATUS_TRIALING = "trialing"
BILLING_STATUS_ACTIVE = "active"
BILLING_STATUS_PAST_DUE = "past_due"
BILLING_STATUS_SUSPENDED = "suspended"
BILLING_STATUS_CANCELED = "canceled"

CURRENT_STATUSES = {BILLING_STATUS_TRIALING, BILLING_STATUS_ACTIVE}


def is_billing_current(org: Organization) -> bool:
    """True while trialing/active; False once past_due/suspended/canceled.

    Tolerates a bare object (e.g. a Mock in tests) by defaulting to trialing.
    """
    status = getattr(org, "billing_status", None) or BILLING_STATUS_TRIALING
    return status in CURRENT_STATUSES


async def get_organization_by_provider_customer_id(
    session: AsyncSession,
    customer_id: str,
) -> Organization | None:
    result = await session.execute(
        select(Organization).where(Organization.billing_provider_customer_id == customer_id)
    )
    return result.scalar_one_or_none()


async def mark_org_paid(
    session: AsyncSession,
    org: Organization,
    *,
    provider_customer_id: str | None = None,
) -> Organization:
    """Mark an org paid: active status, now as last payment, +30d due date."""
    now = datetime.utcnow()
    org.billing_status = BILLING_STATUS_ACTIVE
    org.last_payment_at = now
    org.next_payment_due_at = now + timedelta(days=30)
    if provider_customer_id:
        org.billing_provider_customer_id = provider_customer_id
    await session.flush()
    return org


async def mark_org_past_due(
    session: AsyncSession,
    org: Organization,
) -> Organization:
    org.billing_status = BILLING_STATUS_PAST_DUE
    await session.flush()
    return org


async def suspend_org(
    session: AsyncSession,
    org: Organization,
) -> Organization:
    org.billing_status = BILLING_STATUS_SUSPENDED
    await session.flush()
    return org


async def reactivate_org(
    session: AsyncSession,
    org: Organization,
) -> Organization:
    """Re-activate after suspension: back to active with a fresh +30d due date."""
    now = datetime.utcnow()
    org.billing_status = BILLING_STATUS_ACTIVE
    org.last_payment_at = now
    org.next_payment_due_at = now + timedelta(days=30)
    await session.flush()
    return org


async def list_overdue_orgs(
    session: AsyncSession,
    now: datetime | None = None,
) -> list[Organization]:
    """Orgs past their next payment due date that have not been suspended.

    Suspended orgs are intentionally excluded: they are already stopped, and
    showing them as "overdue" would double-flag an already-handled account.
    """
    now = now or datetime.utcnow()
    result = await session.execute(
        select(Organization).where(
            Organization.next_payment_due_at.is_not(None),
            Organization.next_payment_due_at < now,
            Organization.billing_status != BILLING_STATUS_SUSPENDED,
        )
    )
    return list(result.scalars().all())


async def get_organization_by_id_or_slug(
    session: AsyncSession,
    ref: str,
) -> Organization | None:
    """Look up an org by UUID or slug (used by the manage CLI)."""
    try:
        org_id = UUID(ref)
    except ValueError:
        org_id = None
    if org_id is not None:
        result = await session.execute(select(Organization).where(Organization.id == org_id))
        org = result.scalar_one_or_none()
        if org is not None:
            return org
    result = await session.execute(select(Organization).where(Organization.slug == ref))
    return result.scalar_one_or_none()
