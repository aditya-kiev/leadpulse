"""Tenant resolution from HTTP Host header.

Supports two patterns:
1. Auto-provisioned subdomain — ``{slug}.app.leadpulse.ai``
2. Custom domain — ``leads.brokerage.com`` (mapped in Organization.custom_domain)
"""

from uuid import UUID

from sqlalchemy import select

from app.config.settings import settings
from app.database.models import Organization
from app.database.session import async_session_factory


async def resolve_tenant_from_host(host: str | None) -> UUID | None:
    if not host:
        return None

    host = host.split(":")[0].lower()

    # 1 — Try custom domain match first (exact, so it takes priority)
    async with async_session_factory() as session:
        result = await session.execute(
            select(Organization.id).where(
                Organization.custom_domain == host,
                Organization.is_active == True,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return row

    # 2 — Auto-provisioned subdomain: {slug}.app.{domain}
    app_hostname = settings.app_hostname  # e.g., "app.leadpulse.ai"
    if app_hostname and host.endswith("." + app_hostname):
        slug = host[: -len(app_hostname) - 1]
        if "." in slug or not slug:
            return None
        result = await session.execute(
            select(Organization.id).where(
                Organization.slug == slug,
                Organization.is_active == True,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return row

    return None
